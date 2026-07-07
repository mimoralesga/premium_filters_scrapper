import os
import csv
import json
import time
import random
import sys
import requests
from bs4 import BeautifulSoup

# Muchos IDEs bufferean el stdout del proceso hijo: sin esto, los print()
# no aparecen hasta que el buffer se llena o el script termina, dando la
# falsa impresión de que el scraper se colgó.
sys.stdout.reconfigure(line_buffering=True)

BASE = "https://automotriz.premiumfilters.com.co/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Configuraciones de throttling. No hay proxy/IP rotativa disponible, así que
# la única defensa real contra el rate limit es espaciar los requests y
# reintentar con backoff, no "rotar" la sesión (eso no cambia la IP saliente).
DELAY_MIN = 3.0
DELAY_MAX = 6.0
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
RETRY_BASE_DELAY = 5
RETRY_MAX_DELAY = 180
MAX_RETRIES = 8
COOLDOWN_EVERY = 50    # Cada N requests, pausa larga automática (sin input)
COOLDOWN_SECONDS = 120

TIPOS = {"1": "Automoviles", "4": "Motocicletas"}
PROGRESS_FILE = "scraping_progress.json"

def sleep_jitter():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

def _log(mensaje):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {mensaje}")

def _parse_retry_after(header_value):
    if not header_value:
        return None
    try:
        return int(header_value)
    except ValueError:
        return None

class ScraperFatalError(Exception):
    pass

def _get_with_retry(getter, describir):
    # getter() debe devolver un objeto Response. Reintenta con backoff
    # exponencial (o el Retry-After del servidor si lo manda) hasta
    # MAX_RETRIES, y siempre deja rastro en consola de cuánto lleva esperando.
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = getter()
            if r.status_code == 200:
                return r
            retry_after = _parse_retry_after(r.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else min(RETRY_BASE_DELAY * 2 ** (attempt - 1), RETRY_MAX_DELAY)
            _log(f"[!] {describir}: status {r.status_code} (intento {attempt}/{MAX_RETRIES}). Esperando {delay:.0f}s...")
        except Exception as e:
            delay = min(RETRY_BASE_DELAY * 2 ** (attempt - 1), RETRY_MAX_DELAY)
            _log(f"[!] {describir}: error {e} (intento {attempt}/{MAX_RETRIES}). Esperando {delay:.0f}s...")
        time.sleep(delay)
    raise ScraperFatalError(f"Se agotaron los {MAX_RETRIES} reintentos en: {describir}")

def campos_ocultos(soup):
    def g(n):
        el = soup.find("input", {"name": n})
        return el["value"] if el and el.has_attr("value") else ""
    return {
        "__VIEWSTATE": g("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": g("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": g("__EVENTVALIDATION"),
    }

def opciones(soup, el_id):
    sel = soup.find("select", {"id": el_id})
    if not sel:
        return []
    out = []
    for op in sel.find_all("option"):
        val = op.get("value", "")
        if val and val != "-1":
            out.append((val, op.get_text(strip=True)))
    return out

class RateLimitedSession:
    def __init__(self):
        self.request_count = 0
        self._init_session()

    def _init_session(self):
        if hasattr(self, 's'):
            self.s.close()
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.soup = self._safe_get(BASE)

    def _safe_get(self, url):
        r = _get_with_retry(
            lambda: self.s.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)),
            f"GET {url}",
        )
        return BeautifulSoup(r.text, "html.parser")

    def _post_raw(self, target, valores):
        data = campos_ocultos(self.soup)
        data.update({
            "__EVENTTARGET": target, "__EVENTARGUMENT": "",
            "ctl00$main$ddlTipoAplicacion": valores.get("tipo", "-1"),
            "ctl00$main$ddlMarca": valores.get("marca", "-1"),
            "ctl00$main$ddlAnio": valores.get("anio", "-1"),
            "ctl00$main$ddlModelo": valores.get("modelo", "-1"),
            "ctl00$main$ddlCilindraje": valores.get("motor", "-1"),
        })
        r = _get_with_retry(
            lambda: self.s.post(BASE, data=data, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)),
            f"POST {target}",
        )
        self.soup = BeautifulSoup(r.text, "html.parser")

    def maybe_cooldown(self):
        # Pausa larga automática cada COOLDOWN_EVERY requests, sin pedir
        # confirmación. No se toca la sesión/cookies, así que el ViewState
        # del servidor sigue siendo válido al reanudar.
        if self.request_count >= COOLDOWN_EVERY:
            _log(f"[i] {self.request_count} requests realizados. Pausa automática de {COOLDOWN_SECONDS}s...")
            time.sleep(COOLDOWN_SECONDS)
            _log("[i] Reanudando.")
            self.request_count = 0

    def post(self, target, valores):
        self.request_count += 1
        self._post_raw(target, valores)
        sleep_jitter()
        self.maybe_cooldown()
        return self.soup

def detalle(session, pagina, ref):
    url = f"{BASE}Emergentes/{pagina}.aspx?IdReferencia={ref}"
    r = _get_with_retry(
        lambda: session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)),
        f"GET detalle {pagina} {ref}",
    )
    soup = BeautifulSoup(r.text, "html.parser")

    filas, vistas = [], set()
    for tr in soup.find_all("tr"):
        # El tr envoltorio de la tabla anidada concatena todo el texto de las
        # filas internas en su td exterior; hay que saltarlo.
        if tr.find("table"):
            continue
        celdas = [td.get_text(strip=True) for td in tr.find_all("td")]
        celdas = [c for c in celdas if c]
        if len(celdas) >= 2 and tuple(celdas) not in vistas:
            vistas.add(tuple(celdas))
            filas.append(celdas)
    sleep_jitter()
    return filas

def filtros_de_resultado(soup):
    res = []
    for a in soup.select('a[href*="Filtros.aspx?IdReferencia="]'):
        ref = a["href"].split("IdReferencia=")[-1]
        fila = a.find_parent("tr")
        tipo = ""
        if fila:
            tds = fila.find_all("td")
            if tds:
                tipo = tds[0].get_text(strip=True)
        img = f"{BASE}ImagesFilters/{ref}.jpg"
        res.append((tipo, ref, img))
    
    visto, out = set(), []
    for t, r, i in res:
        if r not in visto:
            visto.add(r)
            out.append((t, r, i))
    return out

def load_existing_veh_rows(path):
    filas = set()
    if os.path.exists(path):
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                filas.add(tuple(row[:7]))
    return filas

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("processed_keys", [])), set(data.get("refs_detalladas", []))
    return set(), set()

def save_progress(processed_keys, refs_detalladas):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "processed_keys": list(processed_keys),
            "refs_detalladas": list(refs_detalladas)
        }, f, ensure_ascii=False, indent=2)

def main():
    processed_keys, refs_detalladas = load_progress()

    # Evaluar si los archivos ya existen para no escribir cabeceras de nuevo
    file_exists = os.path.exists("filtros_por_vehiculo.csv")
    filas_existentes = load_existing_veh_rows("filtros_por_vehiculo.csv")

    f_veh = open("filtros_por_vehiculo.csv", "a", newline="", encoding="utf-8-sig")
    f_apl = open("aplicaciones.csv", "a", newline="", encoding="utf-8-sig")
    f_equ = open("equivalencias.csv", "a", newline="", encoding="utf-8-sig")

    w_veh = csv.writer(f_veh)
    w_apl = csv.writer(f_apl)
    w_equ = csv.writer(f_equ)

    if not file_exists:
        w_veh.writerow(["tipo_aplicacion","fabricante","anio","modelo","motor","tipo_filtro","referencia","url_imagen"])
        w_apl.writerow(["referencia","fabricante","modelo","cilindraje"])
        w_equ.writerow(["referencia","fabricante_equivalente","codigo_equivalente"])

    ses = RateLimitedSession()

    try:
        for tipo_val, tipo_nom in TIPOS.items():
            ses.post("ctl00$main$ddlTipoAplicacion", {"tipo": tipo_val})
            marcas = opciones(ses.soup, "main_ddlMarca")

            for marca_val, marca_nom in marcas:
                ses.post("ctl00$main$ddlMarca", {"tipo": tipo_val, "marca": marca_val})
                anios = opciones(ses.soup, "main_ddlAnio")

                for anio_val, anio_nom in anios:
                    ses.post("ctl00$main$ddlAnio", {"tipo": tipo_val, "marca": marca_val, "anio": anio_val})
                    modelos = opciones(ses.soup, "main_ddlModelo")

                    for modelo_val, modelo_nom in modelos:
                        # Crear una clave única para identificar el "chunk" actual del vehículo
                        chunk_key = f"{tipo_val}|{marca_val}|{anio_val}|{modelo_val}"
                        if chunk_key in processed_keys:
                            continue

                        ses.post("ctl00$main$ddlModelo", {
                            "tipo": tipo_val, "marca": marca_val, "anio": anio_val, "modelo": modelo_val
                        })
                        motores = opciones(ses.soup, "main_ddlCilindraje")

                        for motor_val, motor_nom in motores:
                            soup = ses.post("ctl00$main$ddlCilindraje", {
                                "tipo": tipo_val, "marca": marca_val, "anio": anio_val, "modelo": modelo_val, "motor": motor_val
                            })

                            resultados = filtros_de_resultado(soup)
                            if not resultados:
                                print(f"[-] Sin filtros: {marca_nom} {modelo_nom} {anio_nom} {motor_nom}")

                            for tipo_filtro, ref, img in resultados:
                                print(f"[+] Filtro encontrado: {marca_nom} {modelo_nom} {anio_nom} {motor_nom} -> {ref} ({tipo_filtro})")

                                key = (tipo_nom, marca_nom, anio_nom, modelo_nom, motor_nom, tipo_filtro, ref)
                                if key not in filas_existentes:
                                    filas_existentes.add(key)
                                    w_veh.writerow([tipo_nom, marca_nom, anio_nom, modelo_nom, motor_nom, tipo_filtro, ref, img])

                                if ref not in refs_detalladas:
                                    aplicaciones = detalle(ses.s, "Aplicaciones", ref)
                                    equivalencias = detalle(ses.s, "Equivalencias", ref)
                                    refs_detalladas.add(ref)
                                    for fila in aplicaciones:
                                        w_apl.writerow([ref] + fila[:3])
                                    for fila in equivalencias:
                                        w_equ.writerow([ref] + fila[:2])
                                    if aplicaciones or equivalencias:
                                        print(f"    [i] Detalle extraído de {ref}: {len(aplicaciones)} aplicaciones, {len(equivalencias)} equivalencias")
                                    else:
                                        print(f"    [i] {ref}: sin datos de detalle, se pasó de largo")
                                else:
                                    print(f"    [i] {ref}: detalle ya extraído antes, se omite")

                        # Al finalizar por completo el modelo (un chunk lógico), guardamos progreso
                        processed_keys.add(chunk_key)
                        save_progress(processed_keys, refs_detalladas)
                        f_veh.flush()
                        f_apl.flush()
                        f_equ.flush()

        print("\n[✓] Extracción completada de manera segura.")
    except KeyboardInterrupt:
        print("\n[i] Interrumpido por el usuario. El progreso guardado hasta el último modelo completado permite reanudar.")
    except ScraperFatalError as e:
        _log(f"[!] Fallo persistente tras {MAX_RETRIES} reintentos: {e}. Progreso guardado hasta el último modelo completado — volvé a ejecutar el script para reanudar.")
    finally:
        for f in (f_veh, f_apl, f_equ):
            f.close()

if __name__ == "__main__":
    main()