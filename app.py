from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import os
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
import asyncio
import random
from PIL import Image
import qrcode
from io import BytesIO
import html
import fitz
from starlette.middleware.sessions import SessionMiddleware

# ===================== CONFIGURACIÓN =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = "https://aguascalientes-gob-mx-ui-ciudadano.onrender.com"
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "DIGITAL_AGUASCALIENTES.pdf"
PLANTILLA_RECIBO = "Recibo-aguascalientes.pdf"
ENTIDAD = "ags"
PRECIO_PERMISO = 180
TZ = os.getenv("TZ", "America/Mexico_City")

TEMPLATES_DIR = "templates"
STATIC_DIR = "static"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== SISTEMA DE CONSECUTIVOS =====================
CONSECUTIVOS_INICIALES = {
    "recibo_ingreso": 403202608800627,
    "pase_caja": 9000002373220,
    "numero_1": 93161700,
    "numero_2": 47101510
}

def obtener_siguiente_consecutivo(tipo: str) -> int:
    max_intentos = 1000
    
    for intento in range(max_intentos):
        try:
            resp = supabase.table("consecutivos_ags") \
                .select("valor") \
                .eq("tipo", tipo) \
                .order("valor", desc=True) \
                .limit(1) \
                .execute()
            
            if resp.data and len(resp.data) > 0:
                ultimo_valor = int(resp.data[0]["valor"])
                siguiente = ultimo_valor + 1
            else:
                siguiente = CONSECUTIVOS_INICIALES[tipo]
            
            supabase.table("consecutivos_ags").insert({
                "tipo": tipo,
                "valor": siguiente,
                "created_at": datetime.now(ZoneInfo(TZ)).isoformat()
            }).execute()
            
            print(f"[CONSECUTIVO] {tipo}: {siguiente} (intento {intento + 1})")
            return siguiente
            
        except Exception as e:
            error_msg = str(e).lower()
            if "duplicate" in error_msg or "unique" in error_msg:
                print(f"[CONSECUTIVO] {tipo} duplicado en intento {intento + 1}, reintentando...")
                continue
            else:
                print(f"[CONSECUTIVO] Error en {tipo}: {e}")
                raise e
    
    print(f"[CONSECUTIVO] FALLBACK para {tipo} después de {max_intentos} intentos")
    return CONSECUTIVOS_INICIALES[tipo] + random.randint(1000, 9999)

# ===================== SISTEMA DE TIMERS - 36 HORAS =====================
timers_activos = {}
user_folios = {}

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - AGUASCALIENTES\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
            
        user_id = timers_activos[folio]["user_id"]
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - AGUASCALIENTES\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_36h(user_id: int, folio: str):
    async def timer_task():
        start_time = datetime.now()
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} (36 horas)")
        
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)

        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)

        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer 36h iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}")
        return True
    return False

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int):
    return user_folios.get(user_id, [])

# ===================== FUNCIONES AUXILIARES =====================
def limpiar_entrada(texto: str) -> str:
    if not texto:
        return ""
    texto_limpio = ''.join(c for c in texto if c.isalnum() or c.isspace() or c in '-_./')
    return texto_limpio.strip().upper()

def generar_folio_ags():
    prefijo = "654"
    max_intentos = 10000
    
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

        siguiente = (max(usados) + 1) if usados else 1
        
        for intento in range(max_intentos):
            folio_candidato = f"{prefijo}{siguiente}"
            
            if folio_candidato not in existentes:
                verificacion = supabase.table("folios_registrados") \
                    .select("folio") \
                    .eq("folio", folio_candidato) \
                    .execute()
                
                if not verificacion.data:
                    print(f"[FOLIO] Generado exitosamente: {folio_candidato} (intento {intento + 1})")
                    return folio_candidato
                else:
                    print(f"[FOLIO] {folio_candidato} duplicado en DB, probando siguiente...")
                    existentes.add(folio_candidato)
            
            siguiente += 1
        
        print(f"[FOLIO] FALLBACK después de {max_intentos} intentos")
        return f"{prefijo}{random.randint(50000, 99999)}"
        
    except Exception as e:
        print(f"[FOLIO] Error: {e}")
        return f"{prefijo}{random.randint(1, 9999)}"

def formatear_folio_completo(folio: str) -> str:
    año_actual = datetime.now().year
    return f"AGS  / {folio} / {año_actual}"

def generar_qr_simple_ags(folio):
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

def generar_pdf_unificado_ags(datos: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{datos['folio']}_ags.pdf")
    
    try:
        recibo_ingreso = obtener_siguiente_consecutivo("recibo_ingreso")
        pase_caja = obtener_siguiente_consecutivo("pase_caja")
        numero_1 = obtener_siguiente_consecutivo("numero_1")
        numero_2 = obtener_siguiente_consecutivo("numero_2")
        
        serie_completa = datos["serie"]
        ultimos_4_serie = serie_completa[-4:] if len(serie_completa) >= 4 else serie_completa
        
        fecha_hora_dt = datos['fecha_exp_dt']
        hora_formateada = fecha_hora_dt.strftime("%I:%M %p").lower().replace("am", "a. m.").replace("pm", "p. m.")
        fecha_hora_completa = f"{fecha_hora_dt.strftime('%d/%m/%Y')} {hora_formateada}"
        
        rfc_generico = "XAXX010101000"
        
        folio_completo = formatear_folio_completo(datos["folio"])
        
        if os.path.exists(PLANTILLA_PDF):
            doc_permiso = fitz.open(PLANTILLA_PDF)
            pg_permiso = doc_permiso[0]

            coords_ags = {
                "folio": (828, 103, 30),
                "marca": (245, 305, 25, (0, 0, 0)),
                "modelo": (245, 353, 25, (0, 0, 0)),
                "anio": (245, 353, 25, (0, 0, 0)),
                "color": (245, 402, 25, (0, 0, 0)),
                "serie": (245, 450, 25, (0, 0, 0)),
                "motor": (245, 498, 25, (0, 0, 0)),
                "fecha_exp_larga": (350, 543, 25, (0, 0, 0)),
                "fecha_ven_larga": (850, 543, 25, (0, 0, 0)),
            }

            def put(key, value):
                if key not in coords_ags:
                    return
                x, y, s, col = coords_ags[key]
                pg_permiso.insert_text((x, y), str(value), fontsize=s, color=col)

            def insertar_folio_formateado():
                x_base, y, tamaño_fuente = coords_ags["folio"]
                año_actual = datetime.now().year
                texto_completo = f"AGS  / {datos['folio']} / {año_actual}"
                pg_permiso.insert_text((x_base, y), texto_completo, fontsize=tamaño_fuente, color=(1, 0, 0))

            insertar_folio_formateado()
            
            marca_linea = f"{datos['marca']}   {datos['linea']}"
            put("marca", marca_linea)
            put("anio", datos['anio'])
            put("color", datos["color"])
            put("serie", datos["serie"])
            put("motor", datos["motor"])
            
            MESES_MAYUS = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
            
            def fecha_espaciada(dt: datetime) -> str:
                mes_texto = MESES_MAYUS[dt.month - 1]
                return f"{dt.day:02d}   /   {mes_texto}   /   {dt.year}"
            
            put("fecha_exp_larga", fecha_espaciada(datos['fecha_exp_dt']))
            put("fecha_ven_larga", fecha_espaciada(datos['fecha_ven_dt']))
            
            try:
                img_qr = generar_qr_simple_ags(datos["folio"])
                if img_qr:
                    buf = BytesIO()
                    img_qr.save(buf, format="PNG")
                    buf.seek(0)
                    qr_pix = fitz.Pixmap(buf.read())
                    
                    qr_x = 975
                    qr_y = 130
                    qr_width = qr_height = 138
                    
                    rect = fitz.Rect(qr_x, qr_y, qr_x + qr_width, qr_y + qr_height)
                    pg_permiso.insert_image(rect, pixmap=qr_pix, overlay=True)
            except Exception as e:
                print(f"[PDF] Error agregando QR: {e}")
            
        else:
            doc_permiso = fitz.open()
            pg_permiso = doc_permiso.new_page(width=595, height=842)
            pg_permiso.insert_text((50, 50), "PERMISO AGUASCALIENTES (Plantilla no encontrada)", fontsize=20)
        
        if os.path.exists(PLANTILLA_RECIBO):
            doc_recibo = fitz.open(PLANTILLA_RECIBO)
            pg_recibo = doc_recibo[0]
            
            coords_recibo = {
                "recibo_ingreso_1": (469, 62, 10, (0, 0, 0)),
                "recibo_ingreso_2": (462, 771, 8, (0, 0, 0)),
                "serie_folio": (469, 70, 7, (0, 0, 0)),
                "pase_caja": (469, 83, 8, (0, 0, 0)),
                "fecha_hora": (469, 93, 7, (0, 0, 0)),
                "rfc": (70, 165, 8, (0, 0, 0)),
                "nombre": (70, 178, 8, (0, 0, 0)),
                "numero_1": (149, 291, 5, (0, 0, 0)),
                "numero_2": (190, 291, 5, (0, 0, 0)),
            }
            
            x1, y1, s1, col1 = coords_recibo["recibo_ingreso_1"]
            pg_recibo.insert_text((x1, y1), str(recibo_ingreso), fontsize=s1, color=col1, fontname="hebo")
            
            x2, y2, s2, col2 = coords_recibo["recibo_ingreso_2"]
            pg_recibo.insert_text((x2, y2), str(recibo_ingreso), fontsize=s2, color=col2)
            
            serie_folio_texto = f"{ultimos_4_serie}  {datos['folio']}"
            x, y, s, col = coords_recibo["serie_folio"]
            pg_recibo.insert_text((x, y), serie_folio_texto, fontsize=s, color=col)
            
            x, y, s, col = coords_recibo["pase_caja"]
            pg_recibo.insert_text((x, y), str(pase_caja), fontsize=s, color=col)
            
            x, y, s, col = coords_recibo["fecha_hora"]
            pg_recibo.insert_text((x, y), fecha_hora_completa, fontsize=s, color=col)
            
            x, y, s, col = coords_recibo["rfc"]
            pg_recibo.insert_text((x, y), rfc_generico, fontsize=s, color=col)
            
            x, y, s, col = coords_recibo["nombre"]
            pg_recibo.insert_text((x, y), datos["nombre"], fontsize=s, color=col)
            
            x1, y1, s1, col1 = coords_recibo["numero_1"]
            pg_recibo.insert_text((x1, y1), str(numero_1), fontsize=s1, color=col1)
            
            x2, y2, s2, col2 = coords_recibo["numero_2"]
            pg_recibo.insert_text((x2, y2), str(numero_2), fontsize=s2, color=col2)
        else:
            doc_recibo = fitz.open()
            pg_recibo = doc_recibo.new_page(width=595, height=842)
            pg_recibo.insert_text((50, 50), "RECIBO DE PAGO (Plantilla no encontrada)", fontsize=20)
        
        doc_final = fitz.open()
        doc_final.insert_pdf(doc_permiso)
        doc_final.insert_pdf(doc_recibo)
        doc_final.save(out)
        
        doc_final.close()
        doc_permiso.close()
        if os.path.exists(PLANTILLA_RECIBO):
            doc_recibo.close()
        
        print(f"[PDF] ✅ Unificado generado: {out}")
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
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite",
        parse_mode="HTML"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    activos = obtener_folios_usuario(message.from_user.id)
    if activos:
        folios_formateados = [formatear_folio_completo(f) for f in activos]
        await message.answer(
            f"📋 Folios activos: {', '.join(folios_formateados)}\n"
            "(Cada folio tiene su propio timer de 36 horas)\n\n"
            "Paso 1/7: Ingresa la MARCA del vehículo:",
            parse_mode="HTML"
        )
    else:
        await message.answer("Paso 1/7: Ingresa la MARCA del vehículo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = limpiar_entrada(message.text)
    await state.update_data(marca=marca)
    await message.answer("Paso 2/7: Ingresa la LÍNEA/MODELO:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = limpiar_entrada(message.text)
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
    await state.update_data(serie=serie)
    await message.answer("Paso 5/7: Ingresa el NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = limpiar_entrada(message.text)
    await state.update_data(motor=motor)
    await message.answer("Paso 6/7: Ingresa el COLOR:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = limpiar_entrada(message.text)
    await state.update_data(color=color)
    await message.answer("Paso 7/7: Ingresa el NOMBRE COMPLETO del titular:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = limpiar_entrada(message.text)
    
    datos["nombre"] = nombre
    datos["folio"] = generar_folio_ags()

    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz)
    ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = ven.strftime("%d/%m/%Y")
    datos["fecha_exp_dt"] = hoy
    datos["fecha_ven_dt"] = ven

    folio_completo = formatear_folio_completo(datos["folio"])
    
    await message.answer(
        f"🔄 Generando permiso...\n"
        f"📄 Folio: {folio_completo}\n"
        f"👤 Titular: {datos['nombre']}"
    )

    try:
        pdf_path = generar_pdf_unificado_ags(datos)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{datos['folio']}"),
                InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{datos['folio']}")
            ]
        ])

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=f"📄 PERMISO + RECIBO – AGUASCALIENTES\nFolio: {folio_completo}\nExpedición: {datos['fecha_exp']}\nVencimiento: {datos['fecha_ven']}\n\n⏰ TIMER ACTIVO (36 horas)",
            reply_markup=keyboard
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

        await iniciar_timer_36h(message.from_user.id, datos["folio"])

        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n"
            f"📄 Folio: {folio_completo}\n"
            f"💵 Monto: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            "📸 Envía la foto de tu comprobante aquí mismo.\n"
            f"⚠️ Si no pagas en 36 horas, el folio será eliminado automáticamente.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await message.answer(f"❌ ERROR: {e}\n\n📋 Para generar otro permiso use /chuleta")
    finally:
        await state.clear()

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    
    if not folio.startswith("654"):
        await callback.answer("❌ Folio inválido", show_alert=True)
        return
    
    if folio in timers_activos:
        user_con_folio = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        
        try:
            now = datetime.now().isoformat()
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": now
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")
        
        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        
        try:
            folio_completo = formatear_folio_completo(folio)
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - AGUASCALIENTES\n"
                f"📄 Folio: {folio_completo}\n"
                f"Tu permiso está activo para circular.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando al usuario {user_con_folio}: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "TIMER_DETENIDO",
                "fecha_detencion": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")
        
        await callback.answer("⏹️ Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        
        folio_completo = formatear_folio_completo(folio)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\n\n"
            f"📄 Folio: {folio_completo}\n"
            f"El timer de eliminación automática ha sido detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios = obtener_folios_usuario(user_id)
    if not folios:
        await message.answer("ℹ️ No tienes folios pendientes.\n\n📋 Para generar otro permiso use /chuleta")
        return
    
    if len(folios) > 1:
        lista_folios = '\n'.join([f"• {formatear_folio_completo(f)}" for f in folios])
        await message.answer(
            f"📄 MÚLTIPLES FOLIOS ACTIVOS\n\n"
            f"Tienes {len(folios)} folios pendientes:\n{lista_folios}\n\n"
            f"Responde con el NÚMERO DE FOLIO para este comprobante.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
        return
    
    folio = folios[0]
    cancelar_timer_folio(folio)
    
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
        f"⏹️ Timer detenido.\n\n"
        f"📋 Para generar otro permiso use /chuleta"
    )

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    
    if not folio or not folio.startswith("654"):
        await message.answer("⚠️ Formato: SERO654X (folio debe iniciar con 654).\n\n📋 Para generar otro permiso use /chuleta")
        return

    timer_cancelado = cancelar_timer_folio(folio)
    
    now = datetime.now().isoformat()
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN",
            "fecha_comprobante": now
        }).eq("folio", folio).execute()

    folio_completo = formatear_folio_completo(folio)
    if timer_cancelado:
        await message.answer(f"✅ Validación admin exitosa\n📄 Folio: {folio_completo}\n⏹️ Timer detenido\n\n📋 Para generar otro permiso use /chuleta")
    else:
        await message.answer(f"✅ Validación admin exitosa\n📄 Folio: {folio_completo}\n⚠️ Timer ya estaba inactivo\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
            "No tienes folios pendientes de pago.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )
        return
    
    lista_folios = []
    for folio in folios_usuario:
        if folio in timers_activos:
            tiempo_restante = 2160 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
            tiempo_restante = max(0, tiempo_restante)
            horas = tiempo_restante // 60
            minutos = tiempo_restante % 60
            folio_fmt = formatear_folio_completo(folio)
            lista_folios.append(f"• {folio_fmt} ({horas}h {minutos}min restantes)")
        else:
            folio_fmt = formatear_folio_completo(folio)
            lista_folios.append(f"• {folio_fmt} (sin timer)")
    
    await message.answer(
        f"📋 FOLIOS AGUASCALIENTES ACTIVOS ({len(folios_usuario)})\n\n"
        + '\n'.join(lista_folios) +
        f"\n\n⏰ Cada folio tiene timer de 36 horas.\n"
        f"📸 Para enviar comprobante, use imagen.\n\n"
        f"📋 Para generar otro permiso use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Aguascalientes.")

# ===================== FASTAPI + PANEL WEB =====================
async def keep_alive():
    while True:
        await asyncio.sleep(600)
        
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
    _keep_task = asyncio.create_task(keep_alive())
    print(f"[WEBHOOK] {webhook_url}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Bot Permisos AGS", version="6.0.0")

app.add_middleware(SessionMiddleware, secret_key="tu_clave_secreta_super_segura_123456")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_keep_task = None

# ===================== RUTAS WEB (PANEL ADMINISTRACIÓN) =====================

@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/panel/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    # Admin hardcoded
    if username == "admin_ags" and password == "AGS2026seguro":
        request.session["admin"] = True
        request.session["username"] = username
        return RedirectResponse(url="/panel/admin", status_code=303)
    
    return RedirectResponse(url="/panel/login?error=1", status_code=303)

@app.get("/panel/admin", response_class=HTMLResponse)
async def panel_admin(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    return templates.TemplateResponse("panel.html", {"request": request})

@app.get("/panel/admin_folios", response_class=HTMLResponse)
async def admin_folios_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    # Obtener todos los folios de AGS
    folios_data = supabase.table("folios_registrados")\
        .select("*")\
        .eq("entidad", ENTIDAD)\
        .order("fecha_expedicion", desc=True)\
        .execute()
    
    folios = folios_data.data or []
    
    # Calcular estado
    hoy = datetime.now(ZoneInfo(TZ)).date()
    for f in folios:
        try:
            fecha_ven = datetime.fromisoformat(f['fecha_vencimiento']).date()
            f['estado_calc'] = "VIGENTE" if hoy <= fecha_ven else "VENCIDO"
        except:
            f['estado_calc'] = "ERROR"
    
    return templates.TemplateResponse("admin_folios.html", {
        "request": request,
        "folios": folios
    })

@app.get("/panel/admin_tablas", response_class=HTMLResponse)
async def admin_tablas_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    tablas = {
        'folios_registrados': 'Folios Registrados',
        'consecutivos_ags': 'Consecutivos AGS'
    }
    
    return templates.TemplateResponse("admin_tablas.html", {
        "request": request,
        "tablas": tablas
    })

@app.get("/panel/admin_tabla/{tabla}", response_class=HTMLResponse)
async def admin_tabla_detalle(request: Request, tabla: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    try:
        registros_data = supabase.table(tabla).select("*").execute()
        registros = registros_data.data or []
    except:
        registros = []
    
    return templates.TemplateResponse("admin_tabla_detalle.html", {
        "request": request,
        "tabla": tabla,
        "registros": registros
    })

@app.get("/panel/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/panel/login", status_code=303)

# ===================== RUTAS ADICIONALES - EDICIÓN Y ELIMINACIÓN =====================

@app.get("/panel/editar_folio/{folio}", response_class=HTMLResponse)
async def editar_folio_get(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    try:
        res = supabase.table("folios_registrados").select("*").eq("folio", folio).limit(1).execute()
        registro = (res.data or [None])[0]
        
        if not registro:
            return RedirectResponse(url="/panel/admin_folios?error=not_found", status_code=303)
        
        return templates.TemplateResponse("editar_folio.html", {
            "request": request,
            "registro": registro
        })
    except Exception as e:
        print(f"Error obteniendo folio: {e}")
        return RedirectResponse(url="/panel/admin_folios?error=1", status_code=303)

@app.post("/panel/editar_folio/{folio}")
async def editar_folio_post(
    request: Request,
    folio: str,
    marca: str = Form(...),
    linea: str = Form(...),
    anio: str = Form(...),
    numero_serie: str = Form(...),
    numero_motor: str = Form(...),
    color: str = Form(...),
    contribuyente: str = Form(...),
    fecha_expedicion: str = Form(...),
    fecha_vencimiento: str = Form(...),
    estado: str = Form(...)
):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    try:
        supabase.table("folios_registrados").update({
            "marca": marca.upper(),
            "linea": linea.upper(),
            "anio": anio,
            "numero_serie": numero_serie.upper(),
            "numero_motor": numero_motor.upper(),
            "color": color.upper(),
            "contribuyente": contribuyente.upper(),
            "fecha_expedicion": fecha_expedicion,
            "fecha_vencimiento": fecha_vencimiento,
            "estado": estado
        }).eq("folio", folio).execute()
        
        return RedirectResponse(url="/panel/admin_folios?success=1", status_code=303)
    except Exception as e:
        print(f"Error actualizando folio: {e}")
        return RedirectResponse(url=f"/panel/editar_folio/{folio}?error=1", status_code=303)

@app.post("/panel/eliminar_folio/{folio}")
async def eliminar_folio(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    try:
        # Cancelar timer si existe
        cancelar_timer_folio(folio)
        
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        
        return RedirectResponse(url="/panel/admin_folios?deleted=1", status_code=303)
    except Exception as e:
        print(f"Error eliminando folio: {e}")
        return RedirectResponse(url="/panel/admin_folios?error=delete", status_code=303)

@app.get("/panel/registro_admin", response_class=HTMLResponse)
async def registro_admin_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    return templates.TemplateResponse("registro_admin.html", {"request": request})

@app.post("/panel/registro_admin")
async def registro_admin_post(
    request: Request,
    folio: str = Form(None),
    marca: str = Form(...),
    linea: str = Form(...),
    anio: str = Form(...),
    numero_serie: str = Form(...),
    numero_motor: str = Form(...),
    color: str = Form(...),
    contribuyente: str = Form(...),
    fecha_expedicion: str = Form(None),
    fecha_vencimiento: str = Form(None)
):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    
    try:
        tz = ZoneInfo(TZ)
        
        # Si no hay folio, generar uno
        if not folio or folio.strip() == "":
            folio_generado = generar_folio_ags()
        else:
            folio_generado = folio.strip()
        
        # Si no hay fecha de expedición, usar hoy
        if not fecha_expedicion or fecha_expedicion.strip() == "":
            fecha_exp = datetime.now(tz).date()
        else:
            fecha_exp = datetime.fromisoformat(fecha_expedicion).date()
        
        # Si no hay fecha de vencimiento, calcular 30 días después de expedición
        if not fecha_vencimiento or fecha_vencimiento.strip() == "":
            fecha_ven = fecha_exp + timedelta(days=30)
        else:
            fecha_ven = datetime.fromisoformat(fecha_vencimiento).date()
        
        # Preparar datos para PDF
        datos_pdf = {
            "folio": folio_generado,
            "marca": marca.upper(),
            "linea": linea.upper(),
            "anio": anio,
            "serie": numero_serie.upper(),
            "motor": numero_motor.upper(),
            "color": color.upper(),
            "nombre": contribuyente.upper(),
            "fecha_exp": fecha_exp.strftime("%d/%m/%Y"),
            "fecha_ven": fecha_ven.strftime("%d/%m/%Y"),
            "fecha_exp_dt": datetime.combine(fecha_exp, datetime.min.time()).replace(tzinfo=tz),
            "fecha_ven_dt": datetime.combine(fecha_ven, datetime.min.time()).replace(tzinfo=tz)
        }
        
        # Generar PDF
        pdf_path = generar_pdf_unificado_ags(datos_pdf)
        
        # Insertar en base de datos
        supabase.table("folios_registrados").insert({
            "folio": folio_generado,
            "marca": marca.upper(),
            "linea": linea.upper(),
            "anio": anio,
            "numero_serie": numero_serie.upper(),
            "numero_motor": numero_motor.upper(),
            "color": color.upper(),
            "contribuyente": contribuyente.upper(),
            "fecha_expedicion": fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "entidad": ENTIDAD,
            "estado": "VALIDADO_ADMIN",
            "creado_por": request.session.get("username", "admin")
        }).execute()
        
        return RedirectResponse(url=f"/panel/admin_folios?success=created&folio={folio_generado}", status_code=303)
        
    except Exception as e:
        print(f"Error en registro admin: {e}")
        return RedirectResponse(url="/panel/registro_admin?error=1", status_code=303)

# ===================== ENDPOINTS TELEGRAM =====================

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

@app.get("/estado_folio/{folio}", response_class=HTMLResponse)
async def estado_folio(folio: str, request: Request):
    try:
        print(f"[CONSULTA] Consultando folio: {folio}")
        
        folio_limpio = ''.join(c for c in folio if c.isalnum())
        res = supabase.table("folios_registrados").select("*").eq("folio", folio_limpio).limit(1).execute()
        row = (res.data or [None])[0]
        
        if not row:
            return templates.TemplateResponse("resultado_consulta.html", {
                "request": request,
                "folio": folio_limpio,
                "vigente": False,
                "marca": "",
                "linea": "",
                "anio": "",
                "serie": "",
                "motor": "",
                "color": "",
                "nombre": "",
                "expedicion": "",
                "vencimiento": "",
                "no_encontrado": True
            })
        
        hoy = datetime.now(ZoneInfo(TZ)).date()
        fecha_ven = datetime.fromisoformat(row['fecha_vencimiento']).date()
        vigente = hoy <= fecha_ven
        
        fecha_exp = datetime.fromisoformat(row['fecha_expedicion']).strftime('%d/%m/%Y')
        fecha_ven_str = fecha_ven.strftime('%d/%m/%Y')
        
        return templates.TemplateResponse("resultado_consulta.html", {
            "request": request,
            "folio": folio_limpio,
            "vigente": vigente,
            "marca": row.get('marca', ''),
            "linea": row.get('linea', ''),
            "anio": row.get('anio', ''),
            "serie": row.get('numero_serie', ''),
            "motor": row.get('numero_motor', ''),
            "color": row.get('color', ''),
            "nombre": row.get('contribuyente', ''),
            "expedicion": fecha_exp,
            "vencimiento": fecha_ven_str,
            "no_encontrado": False
        })
        
    except Exception as e:
        print(f"[CONSULTA] Error: {e}")
        return HTMLResponse(f"Error: {str(e)}", status_code=500)

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Sistema Permisos Aguascalientes</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ 
                font-family: Arial, sans-serif; 
                margin: 0; 
                padding: 40px; 
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                text-align: center;
            }}
            .container {{ 
                max-width: 600px; 
                margin: 0 auto; 
                background: white; 
                padding: 40px; 
                border-radius: 15px; 
                box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            }}
            h1 {{ color: #2c3e50; margin-bottom: 30px; }}
            .info {{ 
                background: #e8f5e8; 
                padding: 20px; 
                border-radius: 10px; 
                margin: 20px 0; 
                color: #2d5730;
            }}
            a {{ color: #3B5998; text-decoration: none; font-weight: bold; }}
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
                    <li><strong>Versión:</strong> 6.0 - Panel Web + Bot</li>
                    <li><strong>Costo:</strong> ${PRECIO_PERMISO} MXN</li>
                    <li><strong>Tiempo límite:</strong> 36 horas</li>
                    <li><strong>Timers activos:</strong> {len(timers_activos)}</li>
                </ul>
            </div>
            
            <p>Para obtener tu permiso, inicia una conversación en nuestro bot de Telegram.</p>
            <p><a href="/panel/login">→ Acceder al Panel de Administración</a></p>
        </div>
    </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    try:
        test_query = supabase.table("folios_registrados").select("count", count="exact").limit(1).execute()
        db_status = "conectado" if test_query else "error"
        
        bot_info = await bot.get_me()
        bot_status = f"@{bot_info.username}" if bot_info else "error"
        
        return {
            "status": "healthy",
            "timestamp": datetime.now(ZoneInfo(TZ)).isoformat(),
            "version": "6.0 - Panel Web + Bot",
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

if __name__ == "__main__":
    import uvicorn
    print(f"[SISTEMA] Iniciando Bot + Panel Aguascalientes v6.0...")
    print(f"[SISTEMA] Base URL: {BASE_URL}")
    print(f"[SISTEMA] Panel Web: {BASE_URL}/panel/login")
    uvicorn.run(app, host="0.0.0.0", port=8000)
