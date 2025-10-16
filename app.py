from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
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
from jinja2 import Environment, FileSystemLoader
import fitz

# ===================== CONFIGURACIÓN =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = "https://aguascalientes-gob-mx-ui-ciudadano.onrender.com"
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "DIGITAL_AGUASCALIENTES.pdf"
ENTIDAD = "ags"
PRECIO_PERMISO = 180
TZ = os.getenv("TZ", "America/Mexico_City")

# Directorios
TEMPLATES_DIR = "templates"
STATIC_DIR = "static"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# Jinja2 y Supabase
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Bot Telegram
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== SISTEMA DE TIMERS =====================
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [folio1, folio2, ...]}

async def eliminar_folio_automatico(folio: str):
    """Elimina el folio de Supabase y limpia los timers después de 12 horas"""
    try:
        print(f"[TIMER] Eliminando folio {folio} por tiempo agotado (12h)")
        
        # Obtener user_id antes de eliminar
        user_id = timers_activos.get(folio, {}).get("user_id")
        
        # Eliminar de Supabase
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario si es posible
        if user_id:
            with suppress(Exception):
                await bot.send_message(
                    user_id,
                    f"⏰ TIEMPO AGOTADO\n\nEl folio {folio} fue eliminado automáticamente después de 12 horas sin validación.",
                    parse_mode="HTML"
                )
        
        print(f"[TIMER] Folio {folio} eliminado exitosamente")
        
    except Exception as e:
        print(f"[TIMER] Error eliminando folio {folio}: {e}")
    finally:
        limpiar_timer_folio(folio)

async def iniciar_timer_12h(user_id: int, folio: str):
    """Inicia un timer de 12 horas para un folio específico"""
    async def timer_task():
        try:
            print(f"[TIMER] Timer iniciado para folio {folio} - 12 horas")
            await asyncio.sleep(12 * 60 * 60)  # 12 horas = 43200 segundos
            
            # Verificar si el timer aún está activo (no fue cancelado)
            if folio in timers_activos:
                await eliminar_folio_automatico(folio)
                
        except asyncio.CancelledError:
            print(f"[TIMER] Timer cancelado para folio {folio}")
            pass

    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task, 
        "user_id": user_id, 
        "start_time": datetime.now()
    }
    
    # Agregar folio a la lista del usuario
    user_folios.setdefault(user_id, []).append(folio)
    
    print(f"[TIMER] Timer 12h iniciado para folio {folio} (usuario {user_id})")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico (por comando SERO)"""
    if folio in timers_activos:
        try:
            # Cancelar el task
            timers_activos[folio]["task"].cancel()
            user_id = timers_activos[folio]["user_id"]
            
            # Limpiar estructuras de datos
            del timers_activos[folio]
            
            if user_id in user_folios and folio in user_folios[user_id]:
                user_folios[user_id].remove(folio)
                if not user_folios[user_id]:
                    del user_folios[user_id]
            
            print(f"[TIMER] Timer cancelado para folio {folio}")
            return True
            
        except Exception as e:
            print(f"[TIMER] Error cancelando timer para {folio}: {e}")
            return False
    return False

def limpiar_timer_folio(folio: str):
    """Limpia las estructuras de datos del timer (sin cancelar)"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int):
    """Obtiene la lista de folios activos de un usuario"""
    return user_folios.get(user_id, [])

# ===================== FUNCIONES AUXILIARES =====================
def limpiar_entrada(texto: str) -> str:
    if not texto:
        return ""
    texto_limpio = ''.join(c for c in texto if c.isalnum() or c.isspace() or c in '-_./')
    return texto_limpio.strip().upper()

def generar_folio_ags():
    """Genera un nuevo folio único"""
    prefijo = "1210"
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
            siguiente += 3
        return f"{prefijo}{siguiente}"
    except Exception as e:
        print(f"[FOLIO] Error: {e}")
        return f"{prefijo}{random.randint(10000,99999)}"

def formatear_folio_completo(folio: str) -> str:
    """
    Genera el formato completo del folio: A  / 2025 / (folio)
    """
    año_actual = datetime.now().year
    return f"A  / {año_actual} / {folio}"

def generar_qr_simple_ags(folio):
    """Genera QR que apunta al endpoint de consulta"""
    try:
        url_estado = f"{BASE_URL}/estado_folio/{folio}"
        qr = qrcode.QRCode(
            version=None, 
            error_correction=qrcode.constants.ERROR_CORRECT_M, 
            box_size=4, 
            border=1
        )
        qr.add_data(url_estado)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception as e:
        print(f"[QR] Error: {e}")
        return None

def renderizar_resultado_consulta(row, vigente=True):
    """Renderiza el template HTML con los datos del folio"""
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
        
        # Formatear fecha de vencimiento
        fecha_ven = row.get('fecha_vencimiento', '')
        if fecha_ven:
            try:
                fecha_ven_dt = datetime.fromisoformat(fecha_ven)
                fecha_ven = fecha_ven_dt.strftime("%d/%m/%Y")
            except:
                pass
        
        # Generar folio completo con formato A / 2025 / (folio)
        folio_completo = formatear_folio_completo(row.get('folio', ''))
        
        datos = {
            'folio': row.get('folio', ''),
            'folio_completo': folio_completo,  # NUEVO CAMPO
            'marca': row.get('marca', ''),
            'linea': row.get('linea', ''),
            'anio': row.get('anio', ''),
            'serie': row.get('numero_serie', ''),
            'motor': row.get('numero_motor', ''),
            'color': row.get('color', ''),
            'nombre': row.get('contribuyente', ''),
            'vigencia': 'VIGENTE' if vigente else 'VENCIDO',
            'expedicion': fecha_exp,
            'vencimiento': fecha_ven,
            'vigente': vigente
        }
        
        return template.render(**datos)
        
    except Exception as e:
        print(f"[TEMPLATE] Error renderizando: {e}")
        return f"<html><body><h1>Error al renderizar template: {e}</h1></body></html>"

def generar_pdf_ags(datos: dict) -> str:
    """Genera el PDF del permiso con QR y formato de folio completo"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{datos['folio']}_ags.pdf")
    
    try:
        # Generar el folio completo con formato
        folio_completo = formatear_folio_completo(datos["folio"])
        
        if os.path.exists(PLANTILLA_PDF):
            # Usar plantilla existente
            doc = fitz.open(PLANTILLA_PDF)
            pg = doc[0]

            # Coordenadas para texto en plantilla
            coords_ags = {
                "folio": (835, 103, 28),  # x, y, tamaño fuente para A/2025/folio
                "marca": (245, 305, 20, (0, 0, 0)),
                "color": (245, 402, 20, (0, 0, 0)),
                "serie": (245, 450, 20, (0, 0, 0)),
                "motor": (245, 498, 20, (0, 0, 0)),
                "nombre": (708, 498, 20, (0, 0, 0)),
                "fecha_exp_larga": (380, 543, 20, (0, 0, 0)),
                "fecha_ven_larga": (850, 543, 20, (0, 0, 0)),
            }

            def put(key, value):
                if key not in coords_ags:
                    return
                x, y, s, col = coords_ags[key]
                pg.insert_text((x, y), str(value), fontsize=s, color=col)

            # OPCIÓN 1: Insertar todo el folio junto
            def insertar_folio_formateado():
                """Inserta el folio con formato A / 2025 / (folio) TODO EN ROJO"""
                x_base, y, tamaño_fuente = coords_ags["folio"]
                año_actual = datetime.now().year
                
                # Todo junto en una sola inserción
                texto_completo = f"A  / {año_actual} / {datos['folio']}"
                pg.insert_text((x_base, y), texto_completo, fontsize=tamaño_fuente, color=(1, 0, 0))

            # Usar la función personalizada para el folio
            insertar_folio_formateado()
            
            put("marca", datos["marca"])
            
            # MODIFICACIÓN: Año con 8 espacios antes como ya estaba
            modelo_con_anio = f"{datos['linea']}    AÑO: {datos['anio']}"
            pg.insert_text((245, 353), modelo_con_anio, fontsize=20, color=(0, 0, 0))
            
            put("color", datos["color"])
            put("serie", datos["serie"])
            
            # NUEVA MODIFICACIÓN: Motor + 8 espacios + NOMBRE: + nombre
            motor_con_nombre = f"{datos['motor']}        NOMBRE: {datos['nombre']}"
            pg.insert_text((245, 498), motor_con_nombre, fontsize=20, color=(0, 0, 0))
            
            # Ya no usamos put("nombre", datos["nombre"]) porque ya está incluido en la línea del motor
            
            ABR_MES = ["ene","feb","mar","abr","May","Jun","jul","ago","sep","oct","nov","dic"]
            def fecha_larga(dt: datetime) -> str:
                return f"{dt.day:02d} {ABR_MES[dt.month-1]} {dt.year}"
            
            put("fecha_exp_larga", f"{fecha_larga(datos['fecha_exp_dt'])}")
            put("fecha_ven_larga", f"{fecha_larga(datos['fecha_ven_dt'])}")
            
            # Agregar QR
            try:
                img_qr = generar_qr_simple_ags(datos["folio"])
                if img_qr:
                    buf = BytesIO()
                    img_qr.save(buf, format="PNG")
                    buf.seek(0)
                    qr_pix = fitz.Pixmap(buf.read())
                    
                    qr_x, qr_y = 990, 140
                    qr_width = qr_height = 115
                    
                    rect = fitz.Rect(qr_x, qr_y, qr_x + qr_width, qr_y + qr_height)
                    pg.insert_image(rect, pixmap=qr_pix, overlay=True)
            except Exception as e:
                print(f"[PDF] Error agregando QR: {e}")

        else:
            # Crear PDF básico
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            
            # Insertar folio completo formateado en PDF básico - TODO EN ROJO
            año_actual = datetime.now().year
            texto_completo = f"A  / {año_actual} / {datos['folio']}"
            page.insert_text((50, 80), texto_completo, fontsize=20, color=(1, 0, 0))
            
            y_pos = 120
            line_height = 25
            
            # MODIFICACIÓN TAMBIÉN EN PDF BÁSICO: Motor con formato
            motor_con_nombre = f"{datos['motor']}        NOMBRE: {datos['nombre']}" if datos["motor"].upper() != "SIN NUMERO" else f"        NOMBRE: {datos['nombre']}"
            
            texts = [
                f"{datos['marca']} {datos['linea']}",
                f"    AÑO {datos['anio']}",  # 4 espacios antes del año
                datos["color"],
                datos["serie"],
                motor_con_nombre,  # Motor con nombre formateado
                f"Expedición: {datos['fecha_exp']}",
                f"Vencimiento: {datos['fecha_ven']}"
            ]
            
            for text in texts:
                if text:
                    page.insert_text((50, y_pos), text, fontsize=12, color=(0, 0, 0))
                    y_pos += line_height
            
            # QR en PDF básico
            try:
                img_qr = generar_qr_simple_ags(datos["folio"])
                if img_qr:
                    buf = BytesIO()
                    img_qr.save(buf, format="PNG")
                    buf.seek(0)
                    qr_pix = fitz.Pixmap(buf.read())
                    
                    rect = fitz.Rect(400, 100, 515, 215)
                    page.insert_image(rect, pixmap=qr_pix, overlay=True)
            except Exception as e:
                print(f"[PDF] Error QR en PDF básico: {e}")

        doc.save(out)
        doc.close()
        return out
        
    except Exception as e:
        print(f"[PDF] Error crítico: {e}")
        raise e
                
# ===================== ESTADOS DEL BOT =====================
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ===================== HANDLERS DEL BOT =====================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
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
        # Mostrar folios activos con formato completo
        folios_formateados = [formatear_folio_completo(f) for f in activos]
        await message.answer(
            f"📋 Folios activos: {', '.join(folios_formateados)}\n\n"
            "Paso 1/7: Ingresa la MARCA del vehículo:",
            parse_mode="HTML"
        )
    else:
        await message.answer("Paso 1/7: Ingresa la MARCA del vehículo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = limpiar_entrada(message.text)
    if not marca:
        await message.answer("⚠️ Por favor ingresa una marca válida:")
        return
    await state.update_data(marca=marca)
    await message.answer("Paso 2/7: Ingresa la LÍNEA/MODELO:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = limpiar_entrada(message.text)
    if not linea:
        await message.answer("⚠️ Por favor ingresa una línea/modelo válido:")
        return
    await state.update_data(linea=linea)
    await message.answer("Paso 3/7: Ingresa el AÑO (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ El año debe tener 4 dígitos. Intenta de nuevo:")
        return
    await state.update_data(anio=anio)
    await message.answer("Paso 4/7: Ingresa el NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = limpiar_entrada(message.text)
    if not serie:
        await message.answer("⚠️ Por favor ingresa un número de serie válido:")
        return
    await state.update_data(serie=serie)
    await message.answer("Paso 5/7: Ingresa el NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = limpiar_entrada(message.text)
    if not motor:
        await message.answer("⚠️ Por favor ingresa un número de motor válido:")
        return
    await state.update_data(motor=motor)
    await message.answer("Paso 6/7: Ingresa el COLOR:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = limpiar_entrada(message.text)
    if not color:
        await message.answer("⚠️ Por favor ingresa un color válido:")
        return
    await state.update_data(color=color)
    await message.answer("Paso 7/7: Ingresa el NOMBRE COMPLETO del titular:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = limpiar_entrada(message.text)
    if not nombre:
        await message.answer("⚠️ Por favor ingresa un nombre válido:")
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

    # Mostrar folio con formato completo
    folio_completo = formatear_folio_completo(datos["folio"])
    
    await message.answer(
        f"🔄 Generando permiso...\n"
        f"📄 Folio: {folio_completo}\n"
        f"👤 Titular: {datos['nombre']}"
    )

    try:
        # Generar PDF
        pdf_path = generar_pdf_ags(datos)

        # Enviar PDF al usuario con folio formateado
        await message.answer_document(
            FSInputFile(pdf_path),
            caption=f"📄 PERMISO DIGITAL – AGUASCALIENTES\nFolio: {folio_completo}\nExpedición: {datos['fecha_exp']}\nVencimiento: {datos['fecha_ven']}"
        )

        # Guardar en Supabase
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

        # INICIAR TIMER DE 12 HORAS
        await iniciar_timer_12h(message.from_user.id, datos["folio"])

        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n"
            f"📄 Folio: {folio_completo}\n"
            f"💵 Monto: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Tiempo límite: 12 horas\n\n"
            "📸 Envía la foto de tu comprobante aquí mismo.\n"
            f"🔑 ADMIN: Para validar manual, enviar SERO{datos['folio']} (ej. SERO1292)."
        )

    except Exception as e:
        await message.answer(f"❌ ERROR: {e}\n\nIntenta de nuevo con /permiso")
    finally:
        await state.clear()

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios = obtener_folios_usuario(user_id)
    if not folios:
        await message.answer("ℹ️ No tienes folios pendientes. Usa /permiso para iniciar uno nuevo.")
        return
    
    folio = folios[-1]  # Último folio creado
    cancelar_timer_folio(folio)
    
    # Actualizar estado en Supabase
    now = datetime.now().isoformat()
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    folio_completo = formatear_folio_completo(folio)
    await message.answer(
        f"✅ Comprobante recibido\n"
        f"📄 Folio: {folio_completo}\n"
        f"⏹️ Timer detenido."
    )

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    """Comando SERO + folio para validar manualmente y detener timer"""
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    
    if not folio or not folio.startswith("951"):
        await message.answer("⚠️ Formato: SERO951 (folio debe iniciar con 951).")
        return

    # CANCELAR TIMER ESPECÍFICO
    timer_cancelado = cancelar_timer_folio(folio)
    
    # Actualizar estado en Supabase
    now = datetime.now().isoformat()
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    folio_completo = formatear_folio_completo(folio)
    if timer_cancelado:
        await message.answer(f"✅ Validación admin exitosa\n📄 Folio: {folio_completo}\n⏹️ Timer detenido")
    else:
        await message.answer(f"✅ Validación admin exitosa\n📄 Folio: {folio_completo}\n⚠️ Timer ya estaba inactivo")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Aguascalientes. Usa /permiso para iniciar.")

# ===================== FASTAPI =====================
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

app = FastAPI(lifespan=lifespan, title="Bot Permisos AGS Minimal", version="2.0.0")

# Montar archivos estáticos (imágenes)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_keep_task = None

# ===================== ENDPOINTS ESENCIALES =====================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Webhook para recibir mensajes de Telegram"""
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/estado_folio/{folio}", response_class=HTMLResponse)
async def estado_folio(folio: str):
    """ENDPOINT PRINCIPAL: Muestra el template HTML con los datos del folio"""
    try:
        print(f"[CONSULTA] Consultando folio: {folio}")
        
        folio_limpio = ''.join(c for c in folio if c.isalnum())
        res = supabase.table("folios_registrados").select("*").eq("folio", folio_limpio).limit(1).execute()
        row = (res.data or [None])[0]
        
        if not row:
            print(f"[CONSULTA] Folio no encontrado: {folio_limpio}")
            return HTMLResponse("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Folio No Encontrado</title>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body { font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; text-align: center; }
                    .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; }
                    .error { background: #ffebee; color: #c62828; padding: 20px; border-radius: 8px; }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="error">
                        <h2>❌ Folio No Encontrado</h2>
                        <p>El folio consultado no existe en el sistema.</p>
                    </div>
                </div>
            </body>
            </html>
            """, status_code=404)
        
        # Verificar vigencia
        hoy = datetime.now(ZoneInfo(TZ)).date()
        fecha_ven = datetime.fromisoformat(row['fecha_vencimiento']).date()
        vigente = hoy <= fecha_ven
        
        print(f"[CONSULTA] Folio encontrado: {folio_limpio}, Vigente: {vigente}")
        
        # Renderizar template
        html_resultado = renderizar_resultado_consulta(row, vigente)
        return HTMLResponse(html_resultado)
        
    except Exception as e:
        print(f"[CONSULTA] Error: {e}")
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Error del Sistema</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; text-align: center; }}
                .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; }}
                .error {{ background: #ffebee; color: #c62828; padding: 20px; border-radius: 8px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="error">
                    <h2>⚠️ Error del Sistema</h2>
                    <p>Ocurrió un error al consultar el folio. Intenta más tarde.</p>
                    <p><small>Error: {str(e)}</small></p>
                </div>
            </div>
        </body>
        </html>
        """, status_code=500)

@app.get("/", response_class=HTMLResponse)
async def root():
    """Página de inicio simple"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Sistema Permisos Aguascalientes</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { 
                font-family: Arial, sans-serif; 
                margin: 0; 
                padding: 40px; 
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                text-align: center;
            }
            .container { 
                max-width: 600px; 
                margin: 0 auto; 
                background: white; 
                padding: 40px; 
                border-radius: 15px; 
                box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            }
            h1 { color: #2c3e50; margin-bottom: 30px; }
            .info { 
                background: #e8f5e8; 
                padding: 20px; 
                border-radius: 10px; 
                margin: 20px 0; 
                color: #2d5730;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🏛️ Sistema Digital de Permisos</h1>
            <h2>Aguascalientes</h2>
            
            <div class="info">
                <h3>📊 Estado del Sistema</h3>
                <ul style="text-align: left;">
                    <li><strong>Estado:</strong> ✅ En línea</li>
                    <li><strong>Costo:</strong> $180 MXN</li>
                    <li><strong>Tiempo límite:</strong> 12 horas</li>
                    <li><strong>Timers activos:</strong> {len(timers_activos)}</li>
                </ul>
            </div>
            
            <p>Para obtener tu permiso, inicia una conversación en nuestro bot de Telegram.</p>
        </div>
    </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    """Health check para monitoreo"""
    try:
        # Verificar conexión a Supabase
        test_query = supabase.table("folios_registrados").select("count", count="exact").limit(1).execute()
        db_status = "conectado" if test_query else "error"
        
        # Verificar bot
        bot_info = await bot.get_me()
        bot_status = f"@{bot_info.username}" if bot_info else "error"
        
        return {
            "status": "healthy",
            "timestamp": datetime.now(ZoneInfo(TZ)).isoformat(),
            "services": {
                "database": db_status,
                "telegram_bot": bot_status,
                "timers_activos": len(timers_activos),
                "usuarios_con_folios": len(user_folios)
            }
        }
    except Exception as e:
        return {
            "status": "error", 
            "error": str(e),
            "timestamp": datetime.now(ZoneInfo(TZ)).isoformat()
        }

# ===================== EJECUTAR SERVIDOR =====================
if __name__ == "__main__":
    import uvicorn
    print(f"[SISTEMA] Iniciando Bot Permisos Aguascalientes Minimal...")
    print(f"[SISTEMA] Base URL: {BASE_URL}")
    print(f"[SISTEMA] Entidad: {ENTIDAD}")
    print(f"[SISTEMA] Precio: ${PRECIO_PERMISO} MXN")
    print(f"[SISTEMA] Directorio static: {STATIC_DIR}")
    print(f"[SISTEMA] Directorio templates: {TEMPLATES_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
