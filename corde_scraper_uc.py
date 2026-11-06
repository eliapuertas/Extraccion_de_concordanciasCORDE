import argparse
import logging
import os
import re
import time
from datetime import date, datetime
from itertools import zip_longest
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, NoAlertPresentException, TimeoutException
import undetected_chromedriver as uc

# Regex de formateo del CORDE
REGEX_PATTERN_1 = re.compile(r'(?<=\d)\s{2,}')
REGEX_PATTERN_2 = re.compile(r'\s(?=\d+\.[A-Z].+)')
REGEX_PATTERN_3 = re.compile(r'\s{2,}')
REGEX_PATTERN_4 = re.compile(r'\s(?=[A-Z])')
REGEX_TITULO_PAIS = re.compile(r'\s(?=[A-ZÁÉÍÓÚÑ]{2,}(?:\s|$))')

def configurar_driver(navegador='chrome'):
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    try:
        driver = uc.Chrome(options=options, version_main=156)
    except Exception:
        driver = uc.Chrome(options=options)
    return driver

def construir_url_busqueda(texto="", autor="", titulo="", ano1="", ano2="", tipo="concord"):
    destino_map = {'concord': '1', 'doc': '2', 'par': '3', 'agrup': '4'}
    params = {
        "MfcISAPICommand": "buscar",
        "tradQuery": "1",
        "destino": destino_map.get(tipo, '1'),
        "texto": texto or "",
        "autor": autor or "",
        "titulo": titulo or "",
        "ano1": ano1 or "",
        "ano2": ano2 or "",
        "medio": "1000",
        "pais": "1000",
        "tema": "1000"
    }
    return "https://corpus.rae.es/cgi-bin/crpsrvEx.dll?" + urlencode(params)

def parsear_concordancia(ocurrencia: str) -> list[str]:
    if not ocurrencia or not ocurrencia.strip():
        return None
    
    # Intento 1: Parseo estructurado con expresiones regulares
    try:
        first_processing = ocurrencia.split("**")
        if len(first_processing) == 2:
            num_concord, year_autor = first_processing[0].strip(), first_processing[1].strip()
            result = []

            res1 = REGEX_PATTERN_1.split(num_concord)
            if len(res1) == 2:
                result.extend(res1)
                res2 = REGEX_PATTERN_2.split(year_autor)
                if len(res2) == 2:
                    year_autor_2, topic_pub = res2[0].strip(), res2[1].strip()
                    result.extend(REGEX_PATTERN_3.split(year_autor_2))

                    if len(result) >= 5:
                        posible_titulo_pais = result[4]
                        if re.search(REGEX_TITULO_PAIS, posible_titulo_pais):
                            titulo, pais = re.split(REGEX_TITULO_PAIS, posible_titulo_pais)
                            result[4] = titulo.strip()
                            result.insert(5, pais.strip())
                        else:
                            result.insert(5, "")

                    result.extend(REGEX_PATTERN_4.split(topic_pub, maxsplit=1))

                    if len(result) >= 6:
                        while len(result) < 8:
                            result.append("")
                        return result[:8]
    except Exception:
        pass

    # Intento 2 (Fallback): Separación flexible si la estructura del registro varía
    partes = [p.strip() for p in re.split(r'\s{2,}', ocurrencia) if p.strip()]
    if partes:
        num_id = partes[0] if (partes[0].isdigit() or re.match(r'^\d+\.', partes[0])) else "_"
        texto_resto = " ".join(partes[1:]) if num_id != "_" else ocurrencia
        res_fallback = [num_id, texto_resto]
        while len(res_fallback) < 8:
            res_fallback.append("")
        return res_fallback

    return None

class ErrorRAE(Exception):
    """El servidor de la RAE ha devuelto su página de error."""

def aceptar_alerta(driver):
    try:
        driver.switch_to.alert.accept()
    except NoAlertPresentException:
        pass

def esperar_resultados(driver, timeout=120):
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            cuerpo = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            cuerpo = ""

        if "URL mal formada" in cuerpo or "Petición incorrecta" in cuerpo:
            raise ErrorRAE(f"Página de error de la RAE en: {driver.current_url}")

        if driver.find_elements(By.TAG_NAME, "tt"):
            return
        time.sleep(1)
    raise TimeoutException("No aparecieron resultados en el tiempo esperado")

def extraer_concordancias(driver):
    esperar_resultados(driver)

    dropdown = driver.find_element(By.CSS_SELECTOR, "select[name='tipo1']")
    select_obj = Select(dropdown)
    selected = select_obj.first_selected_option.text
    if 'Concordancias' not in selected.strip():
        raise ValueError('La página actual no contiene concordancias.')

    result = []
    id_concordancias = []
    headers_estandar = ["Nº", "Concordancia", "Año", "Autor", "Título", "País", "Tema", "Publicación"]

    while True:
        outer_element = driver.find_element(By.TAG_NAME, "tt")
        
        try:
            header_element = outer_element.find_element(By.TAG_NAME, 'b').text
            headers = re.sub(r'\s+', ' ', header_element).split(' ')
        except Exception:
            headers = headers_estandar
            header_element = ""

        if len(headers) < len(headers_estandar):
            headers.extend(headers_estandar[len(headers):])

        concords = outer_element.text.replace(header_element, '').split('\n')

        clean_concords = []
        for concord in concords:
            processed = parsear_concordancia(concord)
            if processed is not None and len(processed) > 1:
                if processed[0] not in id_concordancias or processed[0] == "_":
                    if processed[0] != "_":
                        id_concordancias.append(processed[0])
                    clean_concords.append(processed)
                else:
                    break

        for clean_concord in clean_concords:
            result.append({header: att for header, att in zip_longest(headers, clean_concord, fillvalue='_')})

        link_elements = driver.find_elements(By.CSS_SELECTOR, "td.texto a[href*='visualizar']")
        siguientes = [element for element in link_elements if 'Siguiente' in element.text]
        
        if len(siguientes) > 0:
            driver.execute_script("window.onbeforeunload = null;")
            pagina_anterior = outer_element
            siguientes[0].click()
            aceptar_alerta(driver)
            del link_elements
            try:
                WebDriverWait(driver, 60).until(EC.staleness_of(pagina_anterior))
            except TimeoutException:
                pass
            esperar_resultados(driver)
        else:
            del link_elements
            break
    return result

def guardar_resultados(resultados: list[dict], format: str = 'excel', output_path: Path = Path(__file__).resolve().parent) -> None:
    if format not in ['excel', 'csv']:
        raise ValueError('Formato no reconocido.')
    if resultados is None or len(resultados) < 1:
        raise ValueError('No hay resultados para exportar (la lista está vacía).')
    df = pd.DataFrame.from_records(resultados)
    today_str = date.today().strftime("%d-%m-%Y")
    final_path = output_path / 'results' / today_str
    final_path.mkdir(parents=True, exist_ok=True)
    
    file_name = datetime.now().strftime("%Y-%m-%d_%H-%M") + ('.csv' if format == 'csv' else '.xlsx')
    archivo_salida = final_path / file_name
    if format == 'csv':
        df.to_csv(archivo_salida, index=False)
    else:
        df.to_excel(archivo_salida, index=False)
    print(f"Resultados guardados con éxito en: {archivo_salida}")

def main():
    parser = argparse.ArgumentParser(description='Script universal para extraer coincidencias del CORDE.')
    parser.add_argument('-b', '--browser', default='chrome', choices=['chrome', 'firefox', 'edge'], help='Navegador a utilizar.')
    parser.add_argument('-t', '--type', default='concord', choices=['concord', 'doc', 'par', 'agrup'], help='Tipo de resultados.')
    parser.add_argument('-f', '--format', default='excel', choices=['csv', 'excel'], help='Formato de salida.')
    parser.add_argument('-o', '--output', type=Path, default=Path(__file__).resolve().parent, help='Directorio de salida.')
    
    # Parámetros de búsqueda directa
    parser.add_argument('--texto', '-q', type=str, default=None, help='Palabra o término a buscar.')
    parser.add_argument('--autor', type=str, default=None, help='Autor a filtrar.')
    parser.add_argument('--titulo', type=str, default=None, help='Título a filtrar.')
    parser.add_argument('--ano1', type=str, default=None, help='Año inicial.')
    parser.add_argument('--ano2', type=str, default=None, help='Año final.')
    
    # Parámetro de URL completa
    parser.add_argument('--url', type=str, default=None, help='URL completa de una búsqueda del CORDE.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Modo detallado.')
    args = parser.parse_args()

    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", force=True)

    driver = configurar_driver(args.browser.lower())

    # Determinar modo de ejecución
    if args.url:
        target_url = args.url
    elif args.texto or args.autor or args.titulo or args.ano1 or args.ano2:
        target_url = construir_url_busqueda(
            texto=args.texto,
            autor=args.autor,
            titulo=args.titulo,
            ano1=args.ano1,
            ano2=args.ano2,
            tipo=args.type
        )
    else:
        target_url = None

    if target_url:
        logging.info(f"Navegando a la consulta: {target_url}")
        driver.get(target_url)
    else:
        driver.get('https://corpus.rae.es/cordenet.html')
        try:
            _ = WebDriverWait(driver, 300).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='submit'][value='Recuperar']")))
        except Exception as e:
            logging.error("No se cargó el formulario a tiempo.")

        submit_btn = driver.find_element(By.CSS_SELECTOR, "input[type='submit'][value='Recuperar']")
        logging.info('Esperando a que envíes la búsqueda haciendo clic en <<Recuperar>> en el navegador...')
        WebDriverWait(driver, 300).until(EC.staleness_of(submit_btn))

    logging.info('Analizando los resultados en cada página...')
    try:
        resultados = extraer_concordancias(driver)
        guardar_resultados(resultados, args.format, args.output)
    except ErrorRAE as e:
        print(f"[error] {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()