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

# Configuraciones de throttling
DELAY_MIN = 1.5
DELAY_MAX = 3.5
RETRY_DELAY = 15
SESSION_RENEW_EVERY = 50  # Recrear la sesión cada 50 POSTs

TIPOS = {"1": "Automoviles", "4": "Motocicletas"}
PROGRESS_FILE = "scraping_progress.json"

def sleep_jitter():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

def pedir_confirmacion(mensaje):
    while True:
        resp = input(mensaje).strip().lower()
        if resp in ("y", "yes", "s", "si", "sí"):
            return True
        if resp in ("n", "no"):
            return False
        print("[!] Respuesta no válida, escribí Y o N.")

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

LEVELS = [
    ("ctl00$main$ddlTipoAplicacion", "tipo"),
    ("ctl00$main$ddlMarca", "marca"),
    ("ctl00$main$ddlAnio", "anio"),
    ("ctl00$main$ddlModelo", "modelo"),
]

class RateLimitedSession:
    def __init__(self):
        self.request_count = 0
        self.pending_replay = False
        self._init_session()

    def _init_session(self):
        if hasattr(self, 's'):
            self.s.close()
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.soup = self._safe_get(BASE)

    def _safe_get(self, url):
        while True:
            try:
                r = self.s.get(url, timeout=30)
                if r.status_code == 200:
                    return BeautifulSoup(r.text, "html.parser")
                print(f"\n[!] Status {r.status_code} on GET. Retrying in {RETRY_DELAY}s...")
            except Exception as e:
                print(f"\n[!] GET Error: {e}. Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)

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

        while True:
            try:
                r = self.s.post(BASE, data=data, timeout=30)
                if r.status_code == 200:
                    self.soup = BeautifulSoup(r.text, "html.parser")
                    return
                print(f"\n[!] Status {r.status_code} on POST. Retrying in {RETRY_DELAY}s...")
            except Exception as e:
                print(f"\n[!] POST Error: {e}. Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)

    def _replay_context(self, target, valores):
        # Tras rotar la sesión, el __VIEWSTATE queda "en blanco". Hay que
        # rehacer en orden las selecciones previas (tipo -> marca -> anio ->
        # modelo) para que el servidor vuelva a considerar válidos los
        # valores del postback actual, si no los combos quedan vacíos.
        for level_target, key in LEVELS:
            if level_target == target:
                break
            val = valores.get(key)
            if not val or val == "-1":
                continue
            print(f"[*] Reconstruyendo estado: {key}={val}")
            self._post_raw(level_target, valores)
            sleep_jitter()

    def should_checkpoint(self):
        return self.request_count >= SESSION_RENEW_EVERY

    def rotate_now(self):
        self._init_session()
        self.request_count = 0
        self.pending_replay = True

    def post(self, target, valores):
        self.request_count += 1
        if self.pending_replay:
            self._replay_context(target, valores)
            self.pending_replay = False

        self._post_raw(target, valores)
        sleep_jitter()
        return self.soup

def detalle(session, pagina, ref):
    url = f"{BASE}Emergentes/{pagina}.aspx?IdReferencia={ref}"
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    
    filas = []
    for tr in soup.find_all("tr"):
        celdas = [td.get_text(strip=True) for td in tr.find_all("td")]
        celdas = [c for c in celdas if c]
        if len(celdas) >= 2:
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

                            for tipo_filtro, ref, img in filtros_de_resultado(soup):
                                key = (tipo_nom, marca_nom, anio_nom, modelo_nom, motor_nom, tipo_filtro, ref)
                                if key not in filas_existentes:
                                    filas_existentes.add(key)
                                    w_veh.writerow([tipo_nom, marca_nom, anio_nom, modelo_nom, motor_nom, tipo_filtro, ref, img])
                                    print(f"[+] Match: {marca_nom} {modelo_nom} {anio_nom} {motor_nom} -> {ref}")

                                if ref not in refs_detalladas:
                                    refs_detalladas.add(ref)
                                    for fila in detalle(ses.s, "Aplicaciones", ref):
                                        w_apl.writerow([ref] + fila[:3])
                                    for fila in detalle(ses.s, "Equivalencias", ref):
                                        w_equ.writerow([ref] + fila[:2])

                        # Al finalizar por completo el modelo (un chunk lógico), guardamos progreso
                        processed_keys.add(chunk_key)
                        save_progress(processed_keys, refs_detalladas)
                        f_veh.flush()
                        f_apl.flush()
                        f_equ.flush()

                        if ses.should_checkpoint():
                            if not pedir_confirmacion(f"\n[?] {ses.request_count} requests realizados. ¿Continuar? (Y/N): "):
                                print("\n[i] Detenido en un punto seguro. Volvé a ejecutar el script para reanudar.")
                                return
                            ses.rotate_now()

        print("\n[✓] Extracción completada de manera segura.")
    except KeyboardInterrupt:
        print("\n[i] Interrumpido por el usuario. El progreso guardado hasta el último modelo completado permite reanudar.")
    finally:
        for f in (f_veh, f_apl, f_equ):
            f.close()

if __name__ == "__main__":
    main()