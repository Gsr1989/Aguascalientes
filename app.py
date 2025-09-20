from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
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
    return html.escape(str(texto))

def limpiar_entrada(texto: str) -> str:
    if not texto:
        return ""
    texto_limpio = ''.join(c for c in texto if c.isalnum() or c.isspace() or c in '-_./')
    return texto_limpio.strip().upper()

async def enviar_mensaje_seguro(chat_id: int, texto: str, **kwargs):
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
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        if user_id:
            with suppress(Exception):
                await enviar_mensaje_seguro(
                    user_id,
                    f"⏰ TIEMPO AGOTADO\n\nEl folio {folio} fue eliminado por no recibir comprobante ni validación admin en 12 horas.",
                    parse_mode="HTML"
                )
    except Exception as e:
        print(f"[TIMER] Error al eliminar folio {folio}: {e}")
    finally:
        limpiar_timer_folio(folio)

async def iniciar_timer_12h(user_id: int, folio: str):
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
def crear_template_resultado():
    template_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Resultado de Consulta - {{ folio }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            margin: 0;
            padding: 0;
            font-family: Arial, sans-serif;
            background-color: #ffffff;
        }
        
        .container {
            width: 100%;
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
        }
        
        .header {
            width: 100%;
            text-align: center;
        }
        
        .header img {
            width: 100%;
            height: auto;
            display: block;
        }
        
        .content {
            padding: 20px;
            text-align: center;
        }
        
        .resultado-box {
            {% if vigente %}
            background-color: #e8f5e8;
            border: 2px solid #4caf50;
            color: #2d5730;
            {% else %}
            background-color: #ffeaea;
            border: 2px solid #f44336;
            color: #8b2635;
            {% endif %}
            padding: 30px;
            border-radius: 15px;
            margin: 20px 0;
            font-size: 16px;
            line-height: 1.8;
        }
        
        .dato {
            margin: 12px 0;
            font-weight: bold;
            text-align: left;
        }
        
        .boton-salir {
            margin: 30px 0;
        }
        
        .boton-salir a {
            background-color: #2196F3;
            color: white;
            padding: 15px 30px;
            text-decoration: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            display: inline-block;
            transition: background-color 0.3s;
        }
        
        .boton-salir a:hover {
            background-color: #1976D2;
        }
        
        .footer {
            width: 100%;
            text-align: center;
            margin-top: 40px;
        }
        
        .footer img {
            width: 100%;
            height: auto;
            display: block;
        }
        
        @media (max-width: 600px) {
            .container {
                width: 100%;
                margin: 0;
            }
            
            .content {
                padding: 15px;
            }
            
            .resultado-box {
                padding: 20px;
                font-size: 14px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Encabezado -->
        <div class="header">
            <img src="/static/encabezado.png" alt="Encabezado Aguascalientes">
        </div>
        
        <!-- Contenido -->
        <div class="content">
            <div class="resultado-box">
                <div class="dato">Folio: {{ folio }}</div>
                <div class="dato">Marca: {{ marca }}</div>
                <div class="dato">Línea: {{ linea }}</div>
                <div class="dato">Año: {{ anio }}</div>
                <div class="dato">Serie: {{ serie }}</div>
                <div class="dato">Número de motor: {{ motor }}</div>
                <div class="dato">Color: {{ color }}</div>
                <div class="dato">Nombre: {{ nombre }}</div>
                <div class="dato">Vigencia: {{ vigencia }}</div>
                <div class="dato">Expedición: {{ expedicion }}</div>
            </div>
            
            <div class="boton-salir">
                <a href="https://epagos.aguascalientes.gob.mx/contribuciones/default.aspx?opcion=CapturaPlacaSIIF.aspx">Salir</a>
            </div>
        </div>
        
        <!-- Pie de página -->
        <div class="footer">
            <img src="/static/pie.png" alt="Pie Aguascalientes">
        </div>
    </div>
</body>
</html>"""
    
    template_path = os.path.join(TEMPLATES_DIR, "resultado_consulta.html")
    with open(template_path, 'w', encoding='utf-8') as f:
        f.write(template_content)
    return template_path

def renderizar_resultado_consulta(row, vigente=True):
    try:
        template = jinja_env.get_template('resultado_consulta.html')
        
        # Formatear fecha de expedición
        fecha_exp = row.get('fecha_expedicion', '')
        if fecha_exp:
            try:
                fecha_exp_dt = datetime.fromisoformat(fecha_exp)
                fecha_exp = fecha_exp_dt.strftime("%d/%m/%Y")
            except:
                pass
        
        # Agregar fecha actual formateada
        fecha_actual = datetime.now(ZoneInfo(TZ)).strftime("%d/%m/%Y %H:%M:%S")
        
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
            'expedicion': fecha_exp,
            'vigente': vigente,
            'fecha_consulta': fecha_actual
        }
        
        return template.render(**datos)
        
    except Exception as e:
        print(f"Error renderizando template: {e}")
        return f"<html><body><h1>Error al renderizar template: {e}</h1></body></html>"

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

            # QR simplificado
            try:
                img_qr = generar_qr_simple_ags(datos["folio"])
                if img_qr:
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
            except Exception as e:
                print(f"[PDF] Error QR: {e}")

        else:
            print(f"[PDF] Creando desde cero")
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
                print(f"[PDF] Error QR: {e}")

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
        "🏛️ Sistema Digital de Permisos Aguascalientes\n\n"
        f"💰 Costo: ${PRECIO_PERMISO} MXN\n"
        "⏰ Tiempo límite: 12 horas\n"
        "📋 Use /permiso para iniciar su trámite",
        parse_mode="HTML"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    activos = obtener_folios_usuario(message.from_user.id)
    if activos:
        await enviar_mensaje_seguro(
            message.chat.id,
            f"📋 Folios activos: {', '.join(activos)}\n\n"
            "Paso 1/7: Ingresa la MARCA del vehículo:",
            parse_mode="HTML"
        )
    else:
        await enviar_mensaje_seguro(
            message.chat.id,
            "Paso 1/7: Ingresa la MARCA del vehículo:",
            parse_mode="HTML"
        )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = limpiar_entrada(message.text)
    if not marca:
        await enviar_mensaje_seguro(message.chat.id, "⚠️ Por favor ingresa una marca válida:")
        return
    await state.update_data(marca=marca)
    await enviar_mensaje_seguro(message.chat.id, "Paso 2/7: Ingresa la LÍNEA/MODELO:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = limpiar_entrada(message.text)
    if not linea:
        await enviar_mensaje_seguro(message.chat.id, "⚠️ Por favor ingresa una línea/modelo válido:")
        return
    await state.update_data(linea=linea)
    await enviar_mensaje_seguro(message.chat.id, "Paso 3/7: Ingresa el AÑO (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await enviar_mensaje_seguro(message.chat.id, "⚠️ El año debe tener 4 dígitos. Intenta de nuevo:")
        return
    await state.update_data(anio=anio)
    await enviar_mensaje_seguro(message.chat.id, "Paso 4/7: Ingresa el NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = limpiar_entrada(message.text)
    if not serie:
        await enviar_mensaje_seguro(message.chat.id, "⚠️ Por favor ingresa un número de serie válido:")
        return
    await state.update_data(serie=serie)
    await enviar_mensaje_seguro(message.chat.id, "Paso 5/7: Ingresa el NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = limpiar_entrada(message.text)
    if not motor:
        await enviar_mensaje_seguro(message.chat.id, "⚠️ Por favor ingresa un número de motor válido:")
        return
    await state.update_data(motor=motor)
    await enviar_mensaje_seguro(message.chat.id, "Paso 6/7: Ingresa el COLOR:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = limpiar_entrada(message.text)
    if not color:
        await enviar_mensaje_seguro(message.chat.id, "⚠️ Por favor ingresa un color válido:")
        return
    await state.update_data(color=color)
    await enviar_mensaje_seguro(message.chat.id, "Paso 7/7: Ingresa el NOMBRE COMPLETO del titular:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = limpiar_entrada(message.text)
    if not nombre:
        await enviar_mensaje_seguro(message.chat.id, "⚠️ Por favor ingresa un nombre válido:")
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
        f"🔄 Generando permiso...\n"
        f"📄 Folio: {datos['folio']}\n"
        f"👤 Titular: {datos['nombre']}"
    )

    try:
        pdf_path = generar_pdf_ags(datos)

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=f"📄 PERMISO DIGITAL – AGUASCALIENTES\nFolio: {datos['folio']}\nExpedición: {datos['fecha_exp']}\nVencimiento: {datos['fecha_ven']}"
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

        await iniciar_timer_12h(message.from_user.id, datos["folio"])

        await enviar_mensaje_seguro(
            message.chat.id,
            f"💰 INSTRUCCIONES DE PAGO\n"
            f"📄 Folio: {datos['folio']}\n"
            f"💵 Monto: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Tiempo límite: 12 horas\n\n"
            "📸 Envía la foto de tu comprobante aquí mismo.\n"
            "🔑 ADMIN: Para validar manual, enviar SERO<folio> (ej. SERO1292)."
        )

    except Exception as e:
        await enviar_mensaje_seguro(message.chat.id, f"❌ ERROR: {e}\n\nIntenta de nuevo con /permiso")
    finally:
        await state.clear()

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios = obtener_folios_usuario(user_id)
    if not folios:
        await enviar_mensaje_seguro(message.chat.id, "ℹ️ No tienes folios pendientes. Usa /permiso para iniciar uno nuevo.")
        return
    
    folio = folios[-1]
    cancelar_timer_folio(folio)
    now = datetime.now().isoformat()

    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    await enviar_mensaje_seguro(
        message.chat.id,
        f"✅ Comprobante recibido\nFolio: {folio}\n⏹️ Timer detenido."
    )

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    if not folio or not folio.startswith("129"):
        await enviar_mensaje_seguro(message.chat.id, "⚠️ Formato: SERO1292 (folio debe iniciar con 129).")
        return

    cancelar_timer_folio(folio)
    now = datetime.now().isoformat()
    
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    await enviar_mensaje_seguro(message.chat.id, f"✅ Validación admin exitosa\nFolio: {folio}")

@dp.message(lambda m: m.text and any(p in m.text.lower() for p in ["costo","precio","cuanto","pago","monto"]))
async def responder_costo(message: types.Message):
    await enviar_mensaje_seguro(message.chat.id, f"💰 Costo del permiso: ${PRECIO_PERMISO} MXN\nUsa /permiso para iniciar tu trámite.")

@dp.message()
async def fallback(message: types.Message):
    await enviar_mensaje_seguro(message.chat.id, "🏛️ Sistema Digital Aguascalientes. Usa /permiso para iniciar.")

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

# Configurar archivos estáticos desde la raíz del proyecto
app.mount("/static", StaticFiles(directory="."), name="static")

@app.get("/", response_class=HTMLResponse)
async def health():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Sistema Permisos Aguascalientes</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }
            .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h1 { color: #2c3e50; text-align: center; }
            .status { background: #e8f5e8; padding: 15px; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🏛️ Sistema Digital de Permisos - Aguascalientes</h1>
            <div class="status">
                <h3>📊 Estado del Sistema</h3>
                <ul>
                    <li><strong>Estado:</strong> ✅ En línea</li>
                    <li><strong>Costo:</strong> $180 MXN</li>
                </ul>
            </div>
        </div>
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
        return {"ok": False, "error": str(e)}

# Crear el template al inicializar
crear_template_resultado()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
