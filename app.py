from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
import asyncio
import random
from PIL import Image
import qrcode
from io import BytesIO
import html
from jinja2 import Environment, FileSystemLoader, Template

# ===================== CONFIG AGUASCALIENTES =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = "https://aguascalientes-gob-mx-ui-ciudadano.onrender.com"
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "DIGITAL_AGUASCALIENTES.pdf"
ENTIDAD = "ags"
PRECIO_PERMISO = 180
TZ = os.getenv("TZ", "America/Mexico_City")

# Configuración de templates
TEMPLATES_DIR = "templates"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configurar Jinja2
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))

# ===================== SUPABASE =====================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===================== BOT =====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== TIMERS (12 HORAS) =====================
timers_activos = {}
user_folios = {}

# ===================== FUNCIONES AUXILIARES =====================
def sanitizar_texto(texto: str) -> str:
    """Sanitiza texto para evitar problemas con HTML"""
    return html.escape(str(texto))

def limpiar_entrada(texto: str) -> str:
    """Limpia la entrada del usuario removiendo caracteres problemáticos"""
    if not texto:
        return ""
    texto_limpio = ''.join(c for c in texto if c.isalnum() or c.isspace() or c in '-_./')
    return texto_limpio.strip().upper()

async def enviar_mensaje_seguro(chat_id: int, texto: str, **kwargs):
    """Envía mensaje con manejo de errores y fallback"""
    try:
        return await bot.send_message(chat_id, texto, **kwargs)
    except Exception as e:
        print(f"[BOT] Error enviando mensaje formateado: {e}")
        try:
            texto_plano = texto.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '').replace('<code>', '').replace('</code>', '')
            return await bot.send_message(chat_id, texto_plano)
        except Exception as e2:
            print(f"[BOT] Error enviando texto plano: {e2}")
            raise e2

async def eliminar_folio_automatico(folio: str):
    """Borra definitivamente el folio de Supabase y limpia timer."""
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        if user_id:
            with suppress(Exception):
                await enviar_mensaje_seguro(
                    user_id,
                    f"⏰ <b>TIEMPO AGOTADO</b>\n\nEl folio <b>{folio}</b> fue eliminado por no recibir comprobante ni validación admin en 12 horas.",
                    parse_mode="HTML"
                )
    except Exception as e:
        print(f"[TIMER] Error al eliminar folio {folio}: {e}")
    finally:
        limpiar_timer_folio(folio)

async def iniciar_timer_12h(user_id: int, folio: str):
    """Inicia timer exacto de 12 horas"""
    async def timer_task():
        try:
            await asyncio.sleep(12 * 60 * 60)  # 12 horas
            if folio in timers_activos:
                await eliminar_folio_automatico(folio)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[TIMER] Iniciado 12h para {folio} (user {user_id}).")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        with suppress(Exception):
            timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        print(f"[TIMER] Cancelado para {folio}.")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        print(f"[TIMER] Limpiado para {folio}.")

def obtener_folios_usuario(user_id: int):
    return user_folios.get(user_id, [])

# ===================== FUNCIONES DE TEMPLATES =====================
def crear_template_archivo():
    """Crea el archivo de template si no existe"""
    template_html = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>{{ titulo_pagina }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --color-principal: #2c3e50;
            --color-aguascalientes: #e74c3c;
            --color-verde: #27ae60;
            --color-ambar: #f39c12;
            --color-rojo: #e74c3c;
        }
        body {
            background-color: #ffffff;
            font-family: 'Montserrat', Arial, sans-serif;
            margin: 0;
            padding: 0;
            color: var(--color-principal);
        }
        header {
            width: 100%;
            background: linear-gradient(135deg, var(--color-aguascalientes) 0%, #c0392b 100%);
            padding: 20px 0;
            text-align: center;
            color: white;
        }
        .logo { font-size: 48px; margin-bottom: 10px; }
        .titulo {
            text-align: center;
            font-size: 28px;
            margin-top: 20px;
            font-weight: bold;
            color: var(--color-aguascalientes);
        }
        .subtitulo {
            text-align: center;
            font-size: 18px;
            margin-top: 5px;
            color: var(--color-principal);
        }
        .estado {
            text-align: center;
            font-size: 24px;
            font-weight: bold;
            margin-top: 30px;
            padding: 20px;
            border-radius: 15px;
            width: 90%;
            max-width: 600px;
            margin: 30px auto 0;
            color: white;
            box-shadow: 0 8px 25px rgba(0,0,0,0.15);
        }
        .vigente { background: linear-gradient(135deg, var(--color-verde) 0%, #2ecc71 100%); }       
        .vencido { background: linear-gradient(135deg, var(--color-ambar) 0%, #e67e22 100%); }     
        .pendiente { background: linear-gradient(135deg, var(--color-ambar) 0%, #e67e22 100%); }
        .no-encontrado { background: linear-gradient(135deg, var(--color-rojo) 0%, #c0392b 100%); }
        .folio-destacado {
            text-align: center;
            background: var(--color-aguascalientes);
            color: white;
            padding: 20px;
            border-radius: 15px;
            margin: 20px auto;
            max-width: 400px;
            font-size: 20px;
            font-weight: bold;
            letter-spacing: 2px;
        }
        .certificado {
            border: 3px solid #ddd;
            padding: 25px;
            border-radius: 15px;
            width: 90%;
            max-width: 700px;
            margin: 30px auto;
            background-color: #f8f9fa;
            color: var(--color-principal);
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }
        .recuadro-interno {
            border: 2px solid #bdc3c7;
            padding: 25px;
            border-radius: 12px;
            background-color: #ffffff;
        }
        .datos-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .dato {
            margin: 15px 0;
            font-size: 16px;
            color: var(--color-principal);
            padding: 15px;
            background: #ecf0f1;
            border-radius: 8px;
            border-left: 4px solid var(--color-aguascalientes);
        }
        .dato strong {
            display: block;
            font-size: 14px;
            margin-bottom: 8px;
            color: var(--color-principal);
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .dato-valor {
            font-size: 18px;
            font-weight: 600;
            color: #2c3e50;
        }
        .valido {
            text-align: center;
            font-weight: bold;
            margin-top: 30px;
            color: var(--color-principal);
            font-size: 18px;
            background: #e8f5e8;
            padding: 15px;
            border-radius: 10px;
            max-width: 600px;
            margin: 30px auto;
        }
        .acciones {
            width: 90%;
            max-width: 600px;
            margin: 30px auto;
            text-align: center;
        }
        .btn-regresar a {
            display: inline-block;
            padding: 15px 30px;
            border-radius: 10px;
            font-size: 16px;
            color: white;
            background: linear-gradient(135deg, var(--color-principal) 0%, #34495e 100%);
            text-decoration: none;
            transition: all 0.3s ease;
            font-weight: 600;
            margin: 0 10px;
        }
        .btn-regresar a:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(44, 62, 80, 0.3);
        }
        footer {
            margin-top: 50px;
            width: 100%;
            background: var(--color-principal);
            color: white;
            text-align: center;
            padding: 30px 0;
        }
        @media (max-width: 768px) {
            .titulo { font-size: 22px; }
            .estado { font-size: 20px; }
            .datos-grid { grid-template-columns: 1fr; }
            .certificado { padding: 20px; }
        }
    </style>
</head>
<body>
    <header>
        <div class="logo">🏛️</div>
        <h1 style="margin: 0; font-size: 32px;">Gobierno de Aguascalientes</h1>
        <p style="margin: 5px 0 0 0; font-size: 16px; opacity: 0.9;">Sistema Digital de Permisos de Circulación</p>
    </header>

    <div class="titulo">{{ entidad_nombre }}</div>
    <div class="subtitulo">{{ departamento }}</div>

    <div class="folio-destacado">
        FOLIO: {{ resultado.folio }}
    </div>

    {% if resultado.estado == "NO_ENCONTRADO" %}
      <div class="estado no-encontrado">❌ FOLIO NO ENCONTRADO</div>
      <div class="certificado">
        <div class="recuadro-interno">
          <p style="text-align: center; font-size: 18px;">
            El folio <strong>{{ resultado.folio }}</strong> no fue encontrado en el sistema.
          </p>
          <ul style="text-align: left; margin: 20px 0;">
            <li>El folio fue eliminado por vencimiento (12 horas sin comprobante)</li>
            <li>El número de folio es incorrecto</li>
            <li>El permiso aún no ha sido generado</li>
          </ul>
        </div>
      </div>
    {% elif resultado.esta_vencido %}
      <div class="estado vencido">⚠️ FOLIO EXPIRADO</div>
    {% elif resultado.estado in ['VALIDADO_ADMIN', 'COMPROBANTE_ENVIADO'] %}
      <div class="estado vigente">✅ FOLIO VIGENTE</div>
    {% else %}
      <div class="estado pendiente">⏳ FOLIO PENDIENTE</div>
    {% endif %}

    {% if resultado.estado != "NO_ENCONTRADO" %}
      <div class="certificado">
        <div class="recuadro-interno">
          <div class="datos-grid">
            <div class="dato">
              <strong>📅 Fecha de Expedición</strong>
              <div class="dato-valor">{{ resultado.fecha_expedicion }}</div>
            </div>
            <div class="dato">
              <strong>⏰ Fecha de Vencimiento</strong>
              <div class="dato-valor">{{ resultado.fecha_vencimiento }}</div>
            </div>
            <div class="dato">
              <strong>🚗 Marca</strong>
              <div class="dato-valor">{{ resultado.marca }}</div>
            </div>
            <div class="dato">
              <strong>🏷️ Línea/Modelo</strong>
              <div class="dato-valor">{{ resultado.linea }}</div>
            </div>
            <div class="dato">
              <strong>📅 Año</strong>
              <div class="dato-valor">{{ resultado.anio }}</div>
            </div>
            <div class="dato">
              <strong>🔢 Número de Serie</strong>
              <div class="dato-valor">{{ resultado.numero_serie }}</div>
            </div>
            <div class="dato">
              <strong>⚙️ Número de Motor</strong>
              <div class="dato-valor">{{ resultado.numero_motor }}</div>
            </div>
            <div class="dato">
              <strong>👤 Titular</strong>
              <div class="dato-valor">{{ resultado.contribuyente }}</div>
            </div>
          </div>
        </div>
      </div>
    {% endif %}

    <div class="valido">DOCUMENTO DIGITAL VÁLIDO EN TODO MÉXICO</div>

    <div class="acciones btn-regresar">
      <a href="{{ url_inicio }}">🏠 Regresar al Portal</a>
      {% if resultado.estado != "NO_ENCONTRADO" %}
      <a href="{{ base_url }}/estado_folio/{{ resultado.folio }}">📱 Ver Estado QR</a>
      {% endif %}
    </div>

    <footer>
        <p><strong>Documento Oficial Generado Digitalmente</strong></p>
        <p>Sistema de Permisos de Circulación - Gobierno de Aguascalientes</p>
        <p>Consulta realizada el {{ fecha_consulta }}</p>
    </footer>
</body>
</html>"""
    
    template_path = os.path.join(TEMPLATES_DIR, "consulta_folio.html")
    if not os.path.exists(template_path):
        with open(template_path, 'w', encoding='utf-8') as f:
            f.write(template_html)
    return template_path

def preparar_datos_folio(row):
    """Prepara los datos del folio para el template"""
    if not row:
        return {
            'folio': '',
            'estado': 'NO_ENCONTRADO',
            'esta_vencido': False
        }
    
    # Verificar si está vencido
    esta_vencido = False
    fecha_ven = row.get('fecha_vencimiento', '')
    if fecha_ven:
        try:
            fecha_ven_dt = datetime.fromisoformat(fecha_ven)
            hoy = datetime.now(ZoneInfo(TZ)).replace(tzinfo=None)
            esta_vencido = hoy > fecha_ven_dt
        except:
            pass
    
    # Formatear fechas
    fecha_exp = row.get('fecha_expedicion', '')
    fecha_ven_formateada = fecha_ven
    
    try:
        if fecha_exp:
            fecha_exp_dt = datetime.fromisoformat(fecha_exp)
            fecha_exp = fecha_exp_dt.strftime("%d/%m/%Y")
    except:
        pass
        
    try:
        if fecha_ven:
            fecha_ven_dt = datetime.fromisoformat(fecha_ven)
            fecha_ven_formateada = fecha_ven_dt.strftime("%d/%m/%Y")
    except:
        pass
    
    return {
        'folio': row.get('folio', ''),
        'estado': row.get('estado', 'DESCONOCIDO'),
        'esta_vencido': esta_vencido,
        'fecha_expedicion': fecha_exp,
        'fecha_vencimiento': fecha_ven_formateada,
        'marca': row.get('marca', ''),
        'linea': row.get('linea', ''),
        'anio': row.get('anio', ''),
        'numero_serie': row.get('numero_serie', ''),
        'numero_motor': row.get('numero_motor', ''),
        'contribuyente': row.get('contribuyente', ''),
        'entidad': row.get('entidad', '').upper()
    }

def renderizar_consulta_folio(folio_data, base_url=""):
    """Renderiza la página de consulta usando el template"""
    
    # Crear template si no existe
    crear_template_archivo()
    
    # Preparar datos para el template
    template_data = {
        'titulo_pagina': f'Permiso {folio_data.get("folio", "")} - Aguascalientes',
        'entidad_nombre': 'GOBIERNO DE AGUASCALIENTES',
        'departamento': 'SISTEMA DIGITAL DE PERMISOS',
        'base_url': base_url,
        'url_inicio': f"{base_url}/",
        'fecha_consulta': datetime.now(ZoneInfo(TZ)).strftime("%d de %B de %Y a las %H:%M horas"),
        'resultado': folio_data
    }
    
    try:
        # Cargar y renderizar template
        template = jinja_env.get_template('consulta_folio.html')
        return template.render(**template_data)
    except Exception as e:
        print(f"Error renderizando template: {e}")
        # Fallback a template directo
        template_path = crear_template_archivo()
        with open(template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()
        template = Template(template_content)
        return template.render(**template_data)

# Función actualizada para usar el nuevo template
def renderizar_resultado_consulta(row, vigente=True):
    template = jinja_env.get_template('resultado_consulta.html')
    
    datos = {
        'folio': row.get('folio', ''),
        'marca': row.get('marca', ''),
        'linea': row.get('linea', ''),
        'anio': row.get('anio', ''),
        'serie': row.get('numero_serie', ''),
        'motor': row.get('numero_motor', ''),
        'color': row.get('color', ''),
        'nombre': row.get('contribuyente', ''),
        'vigencia': 'VIGENTE' if vigente else 'VENCIDO',
        'expedicion': row.get('fecha_expedicion', ''),
        'vigente': vigente
    }
    
    return template.render(**datos)

# ===================== COORDENADAS Y FECHAS =====================
coords_ags = {
    "folio": (520, 120, 14, (1, 0, 0)),
    "marca": (120, 200, 12, (0, 0, 0)),
    "modelo": (120, 220, 12, (0, 0, 0)),
    "color": (120, 240, 12, (0, 0, 0)),
    "serie": (120, 260, 12, (0, 0, 0)),
    "motor": (120, 280, 12, (0, 0, 0)),
    "nombre": (120, 300, 12, (0, 0, 0)),
    "fecha_exp_larga": (120, 320, 12, (0, 0, 0)),
    "fecha_ven_larga": (120, 340, 12, (0, 0, 0)),
}

ABR_MES = ["ene","feb","mar","abr","May","Jun","jul","ago","sep","oct","nov","dic"]

def fecha_larga(dt: datetime) -> str:
    return f"{dt.day:02d} {ABR_MES[dt.month-1]} {dt.year}"

def generar_folio_ags():
    """Prefijo fijo '129' + incremental pegado"""
    prefijo = "129"
    try:
        resp = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", ENTIDAD) \
            .like("folio", f"{prefijo}%") \
            .execute()
        existentes = {r["folio"] for r in (resp.data or []) if r.get("folio")}

        usados = []
        for f in existentes:
            if f.startswith(prefijo) and len(f) > len(prefijo):
                suf = f[len(prefijo):]
                try:
                    usados.append(int(suf))
                except ValueError:
                    pass

        siguiente = (max(usados) + 1) if usados else 2
        while f"{prefijo}{siguiente}" in existentes:
            siguiente += 1
        return f"{prefijo}{siguiente}"
    except Exception as e:
        print(f"[FOLIO] Error: {e}")
        return f"{prefijo}{random.randint(10000,99999)}"

def generar_qr_simple_ags(folio):
    """QR que apunta directamente al endpoint de estado"""
    try:
        url_estado = f"{BASE_URL}/estado_folio/{folio}"
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url_estado)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception as e:
        print(f"[QR] Error: {e}")
        return None

def generar_pdf_ags(datos: dict) -> str:
    """Genera PDF - usa plantilla si existe, sino crea desde cero"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{datos['folio']}_ags.pdf")
    
    try:
        if os.path.exists(PLANTILLA_PDF):
            print(f"[PDF] Usando plantilla: {PLANTILLA_PDF}")
            doc = fitz.open(PLANTILLA_PDF)
            pg = doc[0]

            def put(key, value):
                if key not in coords_ags:
                    return
                x, y, s, col = coords_ags[key]
                pg.insert_text((x, y), str(value), fontsize=s, color=col)

            put("folio", datos["folio"])
            put("marca", datos["marca"])
            put("modelo", datos["linea"])
            put("color", datos["color"])
            put("serie", datos["serie"])
            put("motor", datos["motor"])
            put("nombre", datos["nombre"])
            put("fecha_exp_larga", f"Exp: {fecha_larga(datos['fecha_exp_dt'])}")
            put("fecha_ven_larga", f"Ven: {fecha_larga(datos['fecha_ven_dt'])}")

            # QR simplificado con solo URL
            try:
                img_qr = generar_qr_simple_ags(datos["folio"])
                if img_qr:
                    print("[PDF] QR simplificado generado correctamente")
                    buf = BytesIO()
                    img_qr.save(buf, format="PNG")
                    buf.seek(0)
                    qr_pix = fitz.Pixmap(buf.read())
                    
                    qr_x = 595
                    qr_y = 148
                    qr_width = 115
                    qr_height = 115
                    
                    rect = fitz.Rect(qr_x, qr_y, qr_x + qr_width, qr_y + qr_height)
                    pg.insert_image(rect, pixmap=qr_pix, overlay=True)
                else:
                    print("[PDF] No se pudo generar el QR")
            except Exception as e:
                print(f"[PDF] Error QR con plantilla: {e}")

        else:
            print(f"[PDF] No se encuentra {PLANTILLA_PDF}, creando desde cero")
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            
            page.insert_text((50, 80), datos["folio"], fontsize=20, color=(1, 0, 0))
            
            y_pos = 120
            line_height = 25
            
            marca_modelo = f"{datos['marca']} {datos['linea']}"
            page.insert_text((50, y_pos), marca_modelo, fontsize=12, color=(0, 0, 0))
            y_pos += line_height
            
            page.insert_text((50, y_pos), datos["anio"], fontsize=12, color=(0, 0, 0))
            y_pos += line_height
            
            page.insert_text((50, y_pos), datos["color"], fontsize=12, color=(0, 0, 0))
            y_pos += line_height
            
            page.insert_text((50, y_pos), datos["serie"], fontsize=12, color=(0, 0, 0))
            y_pos += line_height
            
            if datos["motor"] and datos["motor"].upper() != "SIN NUMERO":
                page.insert_text((50, y_pos), datos["motor"], fontsize=12, color=(0, 0, 0))
                y_pos += line_height
            
            page.insert_text((50, y_pos), datos["nombre"], fontsize=12, color=(0, 0, 0))
            y_pos += line_height
            
            # Mostrar ambas fechas
            fecha_expedicion = datos["fecha_exp"].replace("/", " / ")
            fecha_vencimiento = datos["fecha_ven"].replace("/", " / ")
            page.insert_text((50, y_pos), f"Expedición: {fecha_expedicion}", fontsize=12, color=(0, 0, 0))
            y_pos += line_height
            page.insert_text((50, y_pos), f"Vencimiento: {fecha_vencimiento}", fontsize=12, color=(0, 0, 0))
            
            try:
                img_qr = generar_qr_simple_ags(datos["folio"])
                if img_qr:
                    buf = BytesIO()
                    img_qr.save(buf, format="PNG")
                    buf.seek(0)
                    qr_pix = fitz.Pixmap(buf.read())
                    
                    qr_x = 400
                    qr_y = 100
                    qr_width = 115
                    qr_height = 115
                    
                    rect = fitz.Rect(qr_x, qr_y, qr_x + qr_width, qr_y + qr_height)
                    page.insert_image(rect, pixmap=qr_pix, overlay=True)
            except Exception as e:
                print(f"[PDF] Error QR sin plantilla: {e}")

        doc.save(out)
        doc.close()
        return out
        
    except Exception as e:
        print(f"[PDF] Error crítico: {e}")
        raise e

# ===================== FSM =====================
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ===================== HANDLERS =====================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await enviar_mensaje_seguro(
        message.chat.id,
        "🏛️ <b>Sistema Digital de Permisos Aguascalientes</b>\n\n"
        f"💰 <b>Costo:</b> ${PRECIO_PERMISO} MXN\n"
        "⏰ <b>Tiempo límite:</b> 12 horas (si no envía comprobante o clave admin, se elimina)\n"
        "📋 Use /permiso para iniciar su trámite",
        parse_mode="HTML"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    activos = obtener_folios_usuario(message.from_user.id)
    if activos:
        await enviar_mensaje_seguro(
            message.chat.id,
            f"📋 <b>Folios activos:</b> {', '.join(activos)}\n"
            f"Cada folio expira si no envías comprobante en <b>12h</b>.\n\n"
            "<b>Paso 1/7:</b> Ingresa la <b>MARCA</b> del vehículo:",
            parse_mode="HTML"
        )
    else:
        await enviar_mensaje_seguro(
            message.chat.id,
            "<b>Paso 1/7:</b> Ingresa la <b>MARCA</b> del vehículo:",
            parse_mode="HTML"
        )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = limpiar_entrada(message.text)
    if not marca:
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ Por favor ingresa una marca válida:",
            parse_mode="HTML"
        )
        return
    await state.update_data(marca=marca)
    await enviar_mensaje_seguro(
        message.chat.id,
        "<b>Paso 2/7:</b> Ingresa la <b>LÍNEA/MODELO</b>:",
        parse_mode="HTML"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = limpiar_entrada(message.text)
    if not linea:
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ Por favor ingresa una línea/modelo válido:",
            parse_mode="HTML"
        )
        return
    await state.update_data(linea=linea)
    await enviar_mensaje_seguro(
        message.chat.id,
        "<b>Paso 3/7:</b> Ingresa el <b>AÑO (4 dígitos)</b>:",
        parse_mode="HTML"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ El año debe tener 4 dígitos. Intenta de nuevo:",
            parse_mode="HTML"
        )
        return
    await state.update_data(anio=anio)
    await enviar_mensaje_seguro(
        message.chat.id,
        "<b>Paso 4/7:</b> Ingresa el <b>NÚMERO DE SERIE</b>:",
        parse_mode="HTML"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = limpiar_entrada(message.text)
    if not serie:
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ Por favor ingresa un número de serie válido:",
            parse_mode="HTML"
        )
        return
    await state.update_data(serie=serie)
    await enviar_mensaje_seguro(
        message.chat.id,
        "<b>Paso 5/7:</b> Ingresa el <b>NÚMERO DE MOTOR</b>:",
        parse_mode="HTML"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = limpiar_entrada(message.text)
    if not motor:
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ Por favor ingresa un número de motor válido:",
            parse_mode="HTML"
        )
        return
    await state.update_data(motor=motor)
    await enviar_mensaje_seguro(
        message.chat.id,
        "<b>Paso 6/7:</b> Ingresa el <b>COLOR</b>:",
        parse_mode="HTML"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = limpiar_entrada(message.text)
    if not color:
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ Por favor ingresa un color válido:",
            parse_mode="HTML"
        )
        return
    await state.update_data(color=color)
    await enviar_mensaje_seguro(
        message.chat.id,
        "<b>Paso 7/7:</b> Ingresa el <b>NOMBRE COMPLETO del titular</b>:",
        parse_mode="HTML"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = limpiar_entrada(message.text)
    if not nombre:
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ Por favor ingresa un nombre válido:",
            parse_mode="HTML"
        )
        return
    
    datos["nombre"] = nombre
    datos["folio"] = generar_folio_ags()

    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz)
    ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = ven.strftime("%d/%m/%Y")
    datos["fecha_exp_dt"] = hoy
    datos["fecha_ven_dt"] = ven

    await enviar_mensaje_seguro(
        message.chat.id,
        "🔄 <b>Generando permiso...</b>\n\n"
        f"📄 <b>Folio:</b> {datos['folio']}\n"
        f"👤 <b>Titular:</b> {datos['nombre']}\n"
        "Se emitirá con QR que apunta directamente al estado del folio.",
        parse_mode="HTML"
    )

    try:
        pdf_path = generar_pdf_ags({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "serie": datos["serie"],
            "motor": datos["motor"],
            "color": datos["color"],
            "nombre": datos["nombre"],
            "fecha_exp": datos["fecha_exp"],
            "fecha_ven": datos["fecha_ven"],
            "fecha_exp_dt": datos["fecha_exp_dt"],
            "fecha_ven_dt": datos["fecha_ven_dt"],
        })

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=(
                "📄 <b>PERMISO DIGITAL – AGUASCALIENTES</b>\n"
                f"<b>Folio:</b> {datos['folio']}\n"
                f"<b>Expedición:</b> {datos['fecha_exp']}\n"
                f"<b>Vencimiento:</b> {datos['fecha_ven']}\n"
                "🔳 QR para verificación rápida de estado"
            ),
            parse_mode="HTML"
        )

        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "color": datos["color"],
            "contribuyente": datos["nombre"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": ven.date().isoformat(),
            "entidad": ENTIDAD,
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        supabase.table("borradores_registros").upsert({
            "folio": datos["folio"],
            "entidad": ENTIDAD.upper(),
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "color": datos["color"],
            "contribuyente": datos["nombre"],
            "fecha_expedicion": hoy.isoformat(),
            "fecha_vencimiento": ven.isoformat(),
            "estado": "PENDIENTE",
            "user_id": message.from_user.id
        }).execute()

        await iniciar_timer_12h(message.from_user.id, datos["folio"])

        await enviar_mensaje_seguro(
            message.chat.id,
            f"💰 <b>INSTRUCCIONES DE PAGO</b>\n\n"
            f"📄 <b>Folio:</b> {datos['folio']}\n"
            f"💵 <b>Monto:</b> ${PRECIO_PERMISO} MXN\n"
            f"⏰ <b>Tiempo límite:</b> 12 horas (si no envías comprobante, se elimina)\n\n"
            "📸 <b>IMPORTANTE:</b> Envía la <b>foto</b> de tu comprobante aquí mismo para detener el timer.\n"
            "🔑 <b>ADMIN:</b> Para validar manual, enviar <b>SERO&lt;folio&gt;</b> (ej. <code>SERO1292</code>).",
            parse_mode="HTML"
        )

    except Exception as e:
        error_msg = sanitizar_texto(str(e))
        await enviar_mensaje_seguro(
            message.chat.id,
            f"❌ <b>ERROR:</b> {error_msg}\n\nIntenta de nuevo con /permiso",
            parse_mode="HTML"
        )
    finally:
        await state.clear()

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios = obtener_folios_usuario(user_id)
    if not folios:
        await enviar_mensaje_seguro(
            message.chat.id,
            "ℹ️ No tienes folios pendientes. Usa /permiso para iniciar uno nuevo.",
            parse_mode="HTML"
        )
        return
    
    folio = folios[-1]
    cancelar_timer_folio(folio)
    now = datetime.now().isoformat()

    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    with suppress(Exception):
        supabase.table("borradores_registros").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    await enviar_mensaje_seguro(
        message.chat.id,
        f"✅ <b>Comprobante recibido</b>\n\n"
        f"📄 <b>Folio:</b> {folio}\n"
        f"⏹️ Timer detenido. Tu folio se conserva en el sistema mientras verificamos.",
        parse_mode="HTML"
    )

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    if not folio or not folio.startswith("129"):
        await enviar_mensaje_seguro(
            message.chat.id,
            "⚠️ Formato: <code>SERO1292</code> (folio debe iniciar con 129).",
            parse_mode="HTML"
        )
        return

    cancelar_timer_folio(folio)
    now = datetime.now().isoformat()
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()
    with suppress(Exception):
        supabase.table("borradores_registros").update({
            "estado": "VALIDADO_ADMIN",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    await enviar_mensaje_seguro(
        message.chat.id,
        f"✅ <b>Validación admin exitosa</b>\n\n"
        f"📄 <b>Folio:</b> {folio}\n"
        f"⏹️ Timer detenido y folio preservado en Supabase.",
        parse_mode="HTML"
    )

@dp.message(lambda m: m.text and any(p in m.text.lower() for p in ["costo","precio","cuanto","cuánto","pago","monto","depósito","deposito"]))
async def responder_costo(message: types.Message):
    await enviar_mensaje_seguro(
        message.chat.id,
        f"💰 <b>Costo del permiso:</b> ${PRECIO_PERMISO} MXN\nUsa /permiso para iniciar tu trámite.",
        parse_mode="HTML"
    )

@dp.message()
async def fallback(message: types.Message):
    await enviar_mensaje_seguro(
        message.chat.id,
        "🏛️ Sistema Digital Aguascalientes. Usa /permiso para iniciar.",
        parse_mode="HTML"
    )

# ===================== FASTAPI + WEBHOOK =====================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, allowed_updates=["message"])
    _keep_task = asyncio.create_task(keep_alive())
    print(f"[WEBHOOK] {webhook_url}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Bot Permisos AGS", version="1.1.0")

@app.get("/", response_class=HTMLResponse)
async def health():
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Sistema Permisos Aguascalientes</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }}
            .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #2c3e50; text-align: center; }}
            .status {{ background: #e8f5e8; padding: 15px; border-radius: 5px; margin: 20px 0; }}
            .info {{ background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 20px 0; }}
            .search {{ margin: 20px 0; }}
            .search input {{ width: 60%; padding: 10px; font-size: 16px; border: 1px solid #ddd; border-radius: 5px; }}
            .search button {{ padding: 10px 20px; font-size: 16px; background: #2196F3; color: white; border: none; border-radius: 5px; cursor: pointer; }}
            .search button:hover {{ background: #1976D2; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🏛️ Sistema Digital de Permisos - Aguascalientes</h1>
            
            <div class="status">
                <h3>📊 Estado del Sistema</h3>
                <ul>
                    <li><strong>Timers activos:</strong> {len(timers_activos)}</li>
                    <li><strong>Entidad:</strong> {ENTIDAD.upper()}</li>
                    <li><strong>Plantilla PDF:</strong> {PLANTILLA_PDF}</li>
                    <li><strong>Costo:</strong> ${PRECIO_PERMISO} MXN</li>
                    <li><strong>Estado:</strong> ✅ En línea</li>
                </ul>
            </div>

            <div class="info">
                <h3>🔍 Consulta de Folios</h3>
                <p>Puedes consultar el estado de cualquier folio ingresando el número completo:</p>
                <div class="search">
                    <input type="text" id="folioInput" placeholder="Ej: 1292, 1293, etc." />
                    <button onclick="buscarFolio()">Consultar</button>
                </div>
            </div>

            <div class="info">
                <h3>📱 Bot de Telegram</h3>
                <p>Para generar un nuevo permiso, búscanos en Telegram y usa el comando <code>/permiso</code></p>
                <p><strong>Proceso:</strong></p>
                <ol>
                    <li>Envía <code>/permiso</code> al bot</li>
                    <li>Completa los 7 pasos del formulario</li>
                    <li>Recibe tu PDF con QR dinámico</li>
                    <li>Envía comprobante de pago (foto)</li>
                    <li>¡Listo! Tu permiso está validado</li>
                </ol>
            </div>
        </div>

        <script>
            function buscarFolio() {{
                const folio = document.getElementById('folioInput').value.trim();
                if (folio) {{
                    window.location.href = '/consulta_folio/' + folio;
                }} else {{
                    alert('Por favor ingresa un número de folio');
                }}
            }}
            
            document.getElementById('folioInput').addEventListener('keypress', function(e) {{
                if (e.key === 'Enter') {{
                    buscarFolio();
                }}
            }});
        </script>
    </body>
    </html>
    """

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        try:
            body = await request.body()
            print(f"[WEBHOOK] Request body: {body.decode()[:500]}...")
        except:
            pass
        return {"ok": False, "error": str(e)}

# ===================== ENDPOINT DE ESTADO QR =====================
@app.get("/estado_folio/{folio}", response_class=HTMLResponse)
async def estado_folio(folio: str):
    """Endpoint simplificado para mostrar solo el estado del folio"""
    try:
        folio_limpio = ''.join(c for c in folio if c.isalnum())
        res = supabase.table("folios_registrados").select("*").eq("folio", folio_limpio).limit(1).execute()
        row = (res.data or [None])[0]
        
        if not row:
            return HTMLResponse(f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Estado del Folio - Aguascalientes</title>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body {{ font-family: 'Montserrat', sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
                    .card {{ background: white; padding: 40px; border-radius: 20px; box-shadow: 0 20px 40px rgba(0,0,0,0.1); text-align: center; max-width: 400px; width: 100%; }}
                    .status-icon {{ font-size: 60px; margin-bottom: 20px; }}
                    .status-title {{ font-size: 24px; font-weight: bold; color: #e74c3c; margin-bottom: 15px; }}
                    .folio-number {{ font-size: 20px; color: #2c3e50; margin-bottom: 20px; background: #f8f9fa; padding: 10px; border-radius: 10px; }}
                    .message {{ color: #7f8c8d; line-height: 1.6; }}
                    .back-btn {{ background: #3498db; color: white; padding: 12px 24px; text-decoration: none; border-radius: 10px; display: inline-block; margin-top: 20px; font-weight: 500; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="status-icon">❌</div>
                    <div class="status-title">Folio No Encontrado</div>
                    <div class="folio-number">Folio: {folio_limpio}</div>
                    <div class="message">Este folio no existe en el sistema o fue eliminado por vencimiento.</div>
                    <a href="/" class="back-btn">Volver al Inicio</a>
                </div>
            </body>
            </html>
            """, status_code=404)

        # Verificar estado
        estado = row.get('estado', 'DESCONOCIDO')
        fecha_ven = row.get('fecha_vencimiento', '')
        esta_vencido = False
        
        if fecha_ven:
            try:
                fecha_ven_dt = datetime.fromisoformat(fecha_ven)
                hoy = datetime.now(ZoneInfo(TZ)).replace(tzinfo=None)
                esta_vencido = hoy > fecha_ven_dt
            except:
                pass

        if esta_vencido:
            status_color = "#f39c12"
            status_icon = "⚠️"
            status_title = "FOLIO EXPIRADO"
            status_message = f"El folio {folio_limpio} ha expirado y no es válido para circular."
            card_bg = "linear-gradient(135deg, #f39c12 0%, #e67e22 100%)"
        elif estado in ['VALIDADO_ADMIN', 'COMPROBANTE_ENVIADO']:
            status_color = "#27ae60"
            status_icon = "✅"
            status_title = "FOLIO VIGENTE"
            status_message = f"El folio {folio_limpio} se encuentra vigente y válido para circular."
            card_bg = "linear-gradient(135deg, #27ae60 0%, #2ecc71 100%)"
        else:
            status_color = "#f39c12"
            status_icon = "⏳"
            status_title = "FOLIO PENDIENTE"
            status_message = f"El folio {folio_limpio} está pendiente de validación."
            card_bg = "linear-gradient(135deg, #f39c12 0%, #e67e22 100%)"

        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Estado del Folio {folio_limpio} - Aguascalientes</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
            <style>
                body {{ 
                    font-family: 'Montserrat', sans-serif; 
                    margin: 0; 
                    padding: 20px; 
                    background: {card_bg}; 
                    min-height: 100vh; 
                    display: flex; 
                    align-items: center; 
                    justify-content: center; 
                }}
                .card {{ 
                    background: white; 
                    padding: 40px; 
                    border-radius: 20px; 
                    box-shadow: 0 20px 40px rgba(0,0,0,0.2); 
                    text-align: center; 
                    max-width: 400px; 
                    width: 100%; 
                }}
                .status-icon {{ 
                    font-size: 80px; 
                    margin-bottom: 20px; 
                    animation: pulse 2s infinite;
                }}
                @keyframes pulse {{
                    0% {{ transform: scale(1); }}
                    50% {{ transform: scale(1.1); }}
                    100% {{ transform: scale(1); }}
                }}
                .status-title {{ 
                    font-size: 28px; 
                    font-weight: bold; 
                    color: {status_color}; 
                    margin-bottom: 15px; 
                }}
                .folio-number {{ 
                    font-size: 24px; 
                    color: #2c3e50; 
                    margin-bottom: 20px; 
                    background: #f8f9fa; 
                    padding: 15px; 
                    border-radius: 15px; 
                    font-weight: bold;
                    letter-spacing: 2px;
                }}
                .message {{ 
                    color: #34495e; 
                    line-height: 1.6; 
                    font-size: 16px;
                    margin-bottom: 20px;
                }}
                .details {{ 
                    background: #f8f9fa; 
                    padding: 20px; 
                    border-radius: 15px; 
                    margin: 20px 0; 
                    text-align: left;
                }}
                .detail-row {{ 
                    display: flex; 
                    justify-content: space-between; 
                    margin: 8px 0; 
                    padding: 5px 0;
                    border-bottom: 1px solid #ecf0f1;
                }}
                .detail-row:last-child {{ border-bottom: none; }}
                .detail-label {{ 
                    font-weight: 600; 
                    color: #7f8c8d; 
                }}
                .detail-value {{ 
                    color: #2c3e50; 
                    font-weight: 500;
                }}
                .back-btn {{ 
                    background: #3498db; 
                    color: white; 
                    padding: 15px 30px; 
                    text-decoration: none; 
                    border-radius: 10px; 
                    display: inline-block; 
                    margin: 10px 5px; 
                    font-weight: 600;
                    transition: all 0.3s ease;
                }}
                .back-btn:hover {{ 
                    background: #2980b9; 
                    transform: translateY(-2px);
                }}
                .footer {{ 
                    margin-top: 30px; 
                    color: #95a5a6; 
                    font-size: 12px; 
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="status-icon">{status_icon}</div>
                <div class="status-title">{status_title}</div>
                <div class="folio-number">{folio_limpio}</div>
                <div class="message">{status_message}</div>
                
                <div class="details">
                    <div class="detail-row">
                        <span class="detail-label">Titular:</span>
                        <span class="detail-value">{row.get('contribuyente', 'N/A')}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Vehículo:</span>
# ===================== ENDPOINT DE ESTADO QR =====================
@app.get("/estado_folio/{folio}", response_class=HTMLResponse)
async def estado_folio(folio: str):
    """Endpoint simplificado para mostrar solo el estado del folio"""
    try:
        folio_limpio = ''.join(c for c in folio if c.isalnum())
        res = supabase.table("folios_registrados").select("*").eq("folio", folio_limpio).limit(1).execute()
        row = (res.data or [None])[0]
        
        if not row:
            return HTMLResponse(f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Estado del Folio - Aguascalientes</title>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body {{ font-family: 'Montserrat', sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
                    .card {{ background: white; padding: 40px; border-radius: 20px; box-shadow: 0 20px 40px rgba(0,0,0,0.1); text-align: center; max-width: 400px; width: 100%; }}
                    .status-icon {{ font-size: 60px; margin-bottom: 20px; }}
                    .status-title {{ font-size: 24px; font-weight: bold; color: #e74c3c; margin-bottom: 15px; }}
                    .folio-number {{ font-size: 20px; color: #2c3e50; margin-bottom: 20px; background: #f8f9fa; padding: 10px; border-radius: 10px; }}
                    .message {{ color: #7f8c8d; line-height: 1.6; }}
                    .back-btn {{ background: #3498db; color: white; padding: 12px 24px; text-decoration: none; border-radius: 10px; display: inline-block; margin-top: 20px; font-weight: 500; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="status-icon">❌</div>
                    <div class="status-title">Folio No Encontrado</div>
                    <div class="folio-number">Folio: {folio_limpio}</div>
                    <div class="message">Este folio no existe en el sistema o fue eliminado por vencimiento.</div>
                    <a href="/" class="back-btn">Volver al Inicio</a>
                </div>
            </body>
            </html>
            """, status_code=404)

        # Verificar estado
        estado = row.get('estado', 'DESCONOCIDO')
        fecha_ven = row.get('fecha_vencimiento', '')
        esta_vencido = False
        
        if fecha_ven:
            try:
                fecha_ven_dt = datetime.fromisoformat(fecha_ven)
                hoy = datetime.now(ZoneInfo(TZ)).replace(tzinfo=None)
                esta_vencido = hoy > fecha_ven_dt
            except:
                pass

        if esta_vencido:
            status_color = "#f39c12"
            status_icon = "⚠️"
            status_title = "FOLIO EXPIRADO"
            status_message = f"El folio {folio_limpio} ha expirado y no es válido para circular."
            card_bg = "linear-gradient(135deg, #f39c12 0%, #e67e22 100%)"
        elif estado in ['VALIDADO_ADMIN', 'COMPROBANTE_ENVIADO']:
            status_color = "#27ae60"
            status_icon = "✅"
            status_title = "FOLIO VIGENTE"
            status_message = f"El folio {folio_limpio} se encuentra vigente y válido para circular."
            card_bg = "linear-gradient(135deg, #27ae60 0%, #2ecc71 100%)"
        else:
            status_color = "#f39c12"
            status_icon = "⏳"
            status_title = "FOLIO PENDIENTE"
            status_message = f"El folio {folio_limpio} está pendiente de validación."
            card_bg = "linear-gradient(135deg, #f39c12 0%, #e67e22 100%)"

        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Estado del Folio {folio_limpio} - Aguascalientes</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
            <style>
                body {{ 
                    font-family: 'Montserrat', sans-serif; 
                    margin: 0; 
                    padding: 20px; 
                    background: {card_bg}; 
                    min-height: 100vh; 
                    display: flex; 
                    align-items: center; 
                    justify-content: center; 
                }}
                .card {{ 
                    background: white; 
                    padding: 40px; 
                    border-radius: 20px; 
                    box-shadow: 0 20px 40px rgba(0,0,0,0.2); 
                    text-align: center; 
                    max-width: 400px; 
                    width: 100%; 
                }}
                .status-icon {{ 
                    font-size: 80px; 
                    margin-bottom: 20px; 
                    animation: pulse 2s infinite;
                }}
                @keyframes pulse {{
                    0% {{ transform: scale(1); }}
                    50% {{ transform: scale(1.1); }}
                    100% {{ transform: scale(1); }}
                }}
                .status-title {{ 
                    font-size: 28px; 
                    font-weight: bold; 
                    color: {status_color}; 
                    margin-bottom: 15px; 
                }}
                .folio-number {{ 
                    font-size: 24px; 
                    color: #2c3e50; 
                    margin-bottom: 20px; 
                    background: #f8f9fa; 
                    padding: 15px; 
                    border-radius: 15px; 
                    font-weight: bold;
                    letter-spacing: 2px;
                }}
                .message {{ 
                    color: #34495e; 
                    line-height: 1.6; 
                    font-size: 16px;
                    margin-bottom: 20px;
                }}
                .details {{ 
                    background: #f8f9fa; 
                    padding: 20px; 
                    border-radius: 15px; 
                    margin: 20px 0; 
                    text-align: left;
                }}
                .detail-row {{ 
                    display: flex; 
                    justify-content: space-between; 
                    margin: 8px 0; 
                    padding: 5px 0;
                    border-bottom: 1px solid #ecf0f1;
                }}
                .detail-row:last-child {{ border-bottom: none; }}
                .detail-label {{ 
                    font-weight: 600; 
                    color: #7f8c8d; 
                }}
                .detail-value {{ 
                    color: #2c3e50; 
                    font-weight: 500;
                }}
                .back-btn {{ 
                    background: #3498db; 
                    color: white; 
                    padding: 15px 30px; 
                    text-decoration: none; 
                    border-radius: 10px; 
                    display: inline-block; 
                    margin: 10px 5px; 
                    font-weight: 600;
                    transition: all 0.3s ease;
                }}
                .back-btn:hover {{ 
                    background: #2980b9; 
                    transform: translateY(-2px);
                }}
                .footer {{ 
                    margin-top: 30px; 
                    color: #95a5a6; 
                    font-size: 12px; 
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="status-icon">{status_icon}</div>
                <div class="status-title">{status_title}</div>
                <div class="folio-number">{folio_limpio}</div>
                <div class="message">{status_message}</div>
                
                <div class="details">
                    <div class="detail-row">
                        <span class="detail-label">Titular:</span>
                        <span class="detail-value">{row.get('contribuyente', 'N/A')}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Vehículo:</span>
                        <span class="detail-value">{row.get('marca', '')} {row.get('linea', '')}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Año:</span>
                        <span class="detail-value">{row.get('anio', 'N/A')}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Serie:</span>
                        <span class="detail-value">{row.get('numero_serie', 'N/A')}</span>
                    </div>
                </div>
                
                <a href="/consulta_folio/{folio_limpio}" class="back-btn">Ver Detalles Completos</a>
                <a href="/" class="back-btn">Volver al Portal</a>
                
                <div class="footer">
                    Gobierno de Aguascalientes<br>
                    Consulta realizada: {datetime.now(ZoneInfo(TZ)).strftime("%d/%m/%Y %H:%M")}
                </div>
            </div>
        </body>
        </html>
        """, status_code=200)
        
    except Exception as e:
        print(f"[ESTADO] Error: {e}")
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Error - Aguascalientes</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }}
                .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; }}
                .error {{ color: #d32f2f; }}
                .back-btn {{ background: #2196F3; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block; margin-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2 class="error">❌ Error del Sistema</h2>
                <p>Ocurrió un error al consultar el estado del folio. Por favor intenta de nuevo más tarde.</p>
                <a href="/" class="back-btn">Volver al Inicio</a>
            </div>
        </body>
        </html>
        """, status_code=500)

# ===================== ENDPOINT CON TEMPLATES =====================
@app.get("/consulta_folio/{folio}", response_class=HTMLResponse)
async def consulta_folio(folio: str):
    try:
        # Limpiar el folio de entrada
        folio_limpio = ''.join(c for c in folio if c.isalnum())
        
        # Buscar en la base de datos
        res = supabase.table("folios_registrados").select("*").eq("folio", folio_limpio).limit(1).execute()
        row = (res.data or [None])[0]
        
        # Preparar datos para el template
        folio_data = preparar_datos_folio(row)
        if not row:
            folio_data['folio'] = folio_limpio
        
        # Renderizar usando template
        html_content = renderizar_consulta_folio(folio_data, BASE_URL)
        
        return HTMLResponse(html_content, status_code=200 if row else 404)
        
    except Exception as e:
        print(f"[CONSULTA] Error: {e}")
        # Template de error simple
        error_data = {
            'folio': folio,
            'estado': 'ERROR',
            'esta_vencido': False,
            'fecha_expedicion': 'N/A',
            'fecha_vencimiento': 'N/A',
            'marca': 'N/A',
            'linea': 'N/A',
            'anio': 'N/A',
            'numero_serie': 'N/A',
            'numero_motor': 'N/A',
            'contribuyente': 'N/A',
            'entidad': 'N/A'
        }
        html_content = renderizar_consulta_folio(error_data, BASE_URL)
        return HTMLResponse(html_content, status_code=500)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
                                                
                     
