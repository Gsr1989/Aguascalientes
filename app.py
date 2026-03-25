from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
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
import fitz
import traceback

# ===================== CONFIGURACIÓN =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "https://aguascalientes-gob-mx-ui-ciudadano.onrender.com")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "DIGITAL_AGUASCALIENTES.pdf"
PLANTILLA_RECIBO = "Recibo-aguascalientes.pdf"
ENTIDAD = "ags"
PRECIO_PERMISO = 180
TZ = "America/Mexico_City"

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
            
            return siguiente
            
        except Exception as e:
            error_msg = str(e).lower()
            if "duplicate" in error_msg or "unique" in error_msg:
                continue
            else:
                raise e
    
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
                f"El folio {folio} ha sido eliminado por no pagar en 36 horas."
            )
        
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos: int):
    try:
        if folio not in timers_activos:
            return
            
        user_id = timers_activos[folio]["user_id"]
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO - AGUASCALIENTES\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos} min\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe comprobante"
        )
    except Exception as e:
        print(f"Error recordatorio {folio}: {e}")

async def iniciar_timer_36h(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado {folio} (36h)")
        
        await asyncio.sleep(34.5 * 3600)
        if folio in timers_activos:
            await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        
        if folio in timers_activos:
            await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        
        if folio in timers_activos:
            await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        
        if folio in timers_activos:
            await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        
        if folio in timers_activos:
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
    
    print(f"[TIMER] Iniciado {folio}, total: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[TIMER] Cancelado {folio}")
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
    
    try:
        resp = supabase.table("folios_registrados") \
            .select("folio") \
            .like("folio", f"{prefijo}%") \
            .execute()
        
        existentes = {r["folio"] for r in (resp.data or []) if r.get("folio")}
        usados = []
        
        for f in existentes:
            if f.startswith(prefijo) and len(f) > len(prefijo):
                try:
                    usados.append(int(f[len(prefijo):]))
                except:
                    pass
        
        siguiente = (max(usados) + 1) if usados else 1
        
        for _ in range(10000):
            folio_candidato = f"{prefijo}{siguiente}"
            
            if folio_candidato not in existentes:
                verificacion = supabase.table("folios_registrados") \
                    .select("folio") \
                    .eq("folio", folio_candidato) \
                    .execute()
                
                if not verificacion.data:
                    print(f"[FOLIO] Generado: {folio_candidato}")
                    return folio_candidato
            
            siguiente += 1
        
        return f"{prefijo}{random.randint(50000, 99999)}"
        
    except Exception as e:
        print(f"[FOLIO] Error: {e}")
        return f"{prefijo}{random.randint(1, 9999)}"

def formatear_folio_completo(folio: str) -> str:
    año_actual = datetime.now().year
    return f"AGS  / {folio} / {año_actual}"

def generar_qr_simple_ags(folio):
    try:
        url = f"{BASE_URL}/estado_folio/{folio}"
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except:
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
        
        if os.path.exists(PLANTILLA_PDF):
            doc_permiso = fitz.open(PLANTILLA_PDF)
            pg_permiso = doc_permiso[0]

            coords_ags = {
                "folio": (828, 103, 30),
                "marca": (245, 305, 25, (0, 0, 0)),
                "anio": (245, 353, 25, (0, 0, 0)),
                "color": (245, 402, 25, (0, 0, 0)),
                "serie": (245, 450, 25, (0, 0, 0)),
                "motor": (245, 498, 25, (0, 0, 0)),
                "fecha_exp_larga": (350, 543, 25, (0, 0, 0)),
                "fecha_ven_larga": (850, 543, 25, (0, 0, 0)),
            }

            x_base, y, tamaño_fuente = coords_ags["folio"]
            año_actual = datetime.now().year
            texto_folio = f"AGS  / {datos['folio']} / {año_actual}"
            pg_permiso.insert_text((x_base, y), texto_folio, fontsize=tamaño_fuente, color=(1, 0, 0))
            
            marca_linea = f"{datos['marca']}   {datos['linea']}"
            pg_permiso.insert_text(coords_ags["marca"][:2], marca_linea, fontsize=coords_ags["marca"][2], color=coords_ags["marca"][3])
            pg_permiso.insert_text(coords_ags["anio"][:2], datos['anio'], fontsize=coords_ags["anio"][2], color=coords_ags["anio"][3])
            pg_permiso.insert_text(coords_ags["color"][:2], datos["color"], fontsize=coords_ags["color"][2], color=coords_ags["color"][3])
            pg_permiso.insert_text(coords_ags["serie"][:2], datos["serie"], fontsize=coords_ags["serie"][2], color=coords_ags["serie"][3])
            pg_permiso.insert_text(coords_ags["motor"][:2], datos["motor"], fontsize=coords_ags["motor"][2], color=coords_ags["motor"][3])
            
            MESES_MAYUS = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
            
            def fecha_espaciada(dt: datetime) -> str:
                mes_texto = MESES_MAYUS[dt.month - 1]
                return f"{dt.day:02d}   /   {mes_texto}   /   {dt.year}"
            
            pg_permiso.insert_text(coords_ags["fecha_exp_larga"][:2], fecha_espaciada(datos['fecha_exp_dt']), fontsize=coords_ags["fecha_exp_larga"][2], color=coords_ags["fecha_exp_larga"][3])
            pg_permiso.insert_text(coords_ags["fecha_ven_larga"][:2], fecha_espaciada(datos['fecha_ven_dt']), fontsize=coords_ags["fecha_ven_larga"][2], color=coords_ags["fecha_ven_larga"][3])
            
            img_qr = generar_qr_simple_ags(datos["folio"])
            if img_qr:
                buf = BytesIO()
                img_qr.save(buf, format="PNG")
                buf.seek(0)
                qr_pix = fitz.Pixmap(buf.read())
                rect = fitz.Rect(975, 130, 975 + 138, 130 + 138)
                pg_permiso.insert_image(rect, pixmap=qr_pix, overlay=True)
        else:
            doc_permiso = fitz.open()
            pg_permiso = doc_permiso.new_page(width=595, height=842)
            pg_permiso.insert_text((50, 50), "PERMISO AGS (Plantilla no encontrada)", fontsize=20)
        
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
            
            pg_recibo.insert_text(coords_recibo["recibo_ingreso_1"][:2], str(recibo_ingreso), fontsize=coords_recibo["recibo_ingreso_1"][2], color=coords_recibo["recibo_ingreso_1"][3], fontname="hebo")
            pg_recibo.insert_text(coords_recibo["recibo_ingreso_2"][:2], str(recibo_ingreso), fontsize=coords_recibo["recibo_ingreso_2"][2], color=coords_recibo["recibo_ingreso_2"][3])
            pg_recibo.insert_text(coords_recibo["serie_folio"][:2], f"{ultimos_4_serie}  {datos['folio']}", fontsize=coords_recibo["serie_folio"][2], color=coords_recibo["serie_folio"][3])
            pg_recibo.insert_text(coords_recibo["pase_caja"][:2], str(pase_caja), fontsize=coords_recibo["pase_caja"][2], color=coords_recibo["pase_caja"][3])
            pg_recibo.insert_text(coords_recibo["fecha_hora"][:2], fecha_hora_completa, fontsize=coords_recibo["fecha_hora"][2], color=coords_recibo["fecha_hora"][3])
            pg_recibo.insert_text(coords_recibo["rfc"][:2], rfc_generico, fontsize=coords_recibo["rfc"][2], color=coords_recibo["rfc"][3])
            pg_recibo.insert_text(coords_recibo["nombre"][:2], datos["nombre"], fontsize=coords_recibo["nombre"][2], color=coords_recibo["nombre"][3])
            pg_recibo.insert_text(coords_recibo["numero_1"][:2], str(numero_1), fontsize=coords_recibo["numero_1"][2], color=coords_recibo["numero_1"][3])
            pg_recibo.insert_text(coords_recibo["numero_2"][:2], str(numero_2), fontsize=coords_recibo["numero_2"][2], color=coords_recibo["numero_2"][3])
        else:
            doc_recibo = fitz.open()
            pg_recibo = doc_recibo.new_page(width=595, height=842)
            pg_recibo.insert_text((50, 50), "RECIBO (Plantilla no encontrada)", fontsize=20)
        
        doc_final = fitz.open()
        doc_final.insert_pdf(doc_permiso)
        doc_final.insert_pdf(doc_recibo)
        doc_final.save(out)
        
        doc_final.close()
        doc_permiso.close()
        if os.path.exists(PLANTILLA_RECIBO):
            doc_recibo.close()
        
        print(f"[PDF] Generado: {out}")
        return out
        
    except Exception as e:
        print(f"[PDF] Error: {e}")
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
        "🏛️ Sistema Aguascalientes\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        "⏰ 36 horas para pagar"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    activos = obtener_folios_usuario(message.from_user.id)
    msg = "🚗 NUEVO PERMISO AGS\n\n"
    if activos:
        msg += f"Folios activos: {', '.join(activos)}\n\n"
    msg += "Paso 1/7: MARCA del vehículo"
    await message.answer(msg)
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=limpiar_entrada(message.text))
    await message.answer("Paso 2/7: LÍNEA/MODELO")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=limpiar_entrada(message.text))
    await message.answer("Paso 3/7: AÑO (4 dígitos)")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Año inválido. Intenta de nuevo:")
        return
    await state.update_data(anio=anio)
    await message.answer("Paso 4/7: NÚMERO DE SERIE")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=limpiar_entrada(message.text))
    await message.answer("Paso 5/7: NÚMERO DE MOTOR")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=limpiar_entrada(message.text))
    await message.answer("Paso 6/7: COLOR")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=limpiar_entrada(message.text))
    await message.answer("Paso 7/7: NOMBRE COMPLETO")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = limpiar_entrada(message.text)
    datos["folio"] = generar_folio_ags()

    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz)
    ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = ven.strftime("%d/%m/%Y")
    datos["fecha_exp_dt"] = hoy
    datos["fecha_ven_dt"] = ven

    await message.answer(f"🔄 Generando permiso...\nFolio: {datos['folio']}")

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
            caption=f"📄 PERMISO AGS\nFolio: {datos['folio']}\n⏰ TIMER ACTIVO (36h)",
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
            f"💰 PAGO\n\n"
            f"Folio: {datos['folio']}\n"
            f"Monto: ${PRECIO_PERMISO}\n"
            f"⏰ 36 horas\n\n"
            f"📸 Envía comprobante"
        )

    except Exception as e:
        await message.answer(f"❌ ERROR: {e}")
        traceback.print_exc()
    finally:
        await state.clear()

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN"
        }).eq("folio", folio).execute()
        
        await callback.answer("✅ Validado", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await bot.send_message(user_id, f"✅ PAGO VALIDADO\nFolio: {folio}")
    else:
        await callback.answer("❌ No encontrado", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        
        supabase.table("folios_registrados").update({
            "estado": "TIMER_DETENIDO"
        }).eq("folio", folio).execute()
        
        await callback.answer("⏹️ Timer detenido", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
    else:
        await callback.answer("❌ Timer inactivo", show_alert=True)

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def admin_sero(message: types.Message):
    folio = message.text.strip().upper().replace("SERO", "").strip()
    
    if not folio.startswith("654"):
        await message.answer("⚠️ Folio inválido")
        return
    
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN"
        }).eq("folio", folio).execute()
        
        await message.answer(f"✅ Validado: {folio}")
        await bot.send_message(user_id, f"✅ PAGO VALIDADO\nFolio: {folio}")
    else:
        await message.answer(f"❌ Folio no encontrado: {folio}")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    folios = obtener_folios_usuario(message.from_user.id)
    
    if not folios:
        await message.answer("ℹ️ No tienes folios pendientes")
        return
    
    if len(folios) > 1:
        await message.answer(f"Tienes {len(folios)} folios:\n" + '\n'.join([f"• {f}" for f in folios]) + "\n\nResponde con el folio")
        return
    
    folio = folios[0]
    cancelar_timer_folio(folio)
    
    supabase.table("folios_registrados").update({
        "estado": "COMPROBANTE_ENVIADO"
    }).eq("folio", folio).execute()
    
    await message.answer(f"✅ Comprobante recibido\nFolio: {folio}\n⏹️ Timer detenido")

@dp.message(Command("folios"))
async def ver_folios(message: types.Message):
    folios = obtener_folios_usuario(message.from_user.id)
    
    if not folios:
        await message.answer("ℹ️ No tienes folios activos")
        return
    
    lista = []
    for f in folios:
        if f in timers_activos:
            tiempo = 2160 - int((datetime.now() - timers_activos[f]["start_time"]).total_seconds() / 60)
            tiempo = max(0, tiempo)
            h = tiempo // 60
            m = tiempo % 60
            lista.append(f"• {f} ({h}h {m}min)")
        else:
            lista.append(f"• {f} (sin timer)")
    
    await message.answer(f"📋 Folios activos ({len(folios)}):\n\n" + '\n'.join(lista))

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Aguascalientes")

# ===================== FASTAPI =====================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message", "callback_query"])
        _keep_task = asyncio.create_task(keep_alive())
    
    print("[SISTEMA] AGS Bot + Web iniciado")
    
    yield
    
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key="tu_clave_secreta_super_segura_123456")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ===================== RUTAS WEB =====================
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return {"ok": False}

@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/panel/login")

@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    error_param = request.query_params.get("error", "")
    
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": str(error_param) if error_param else ""
    })

@app.post("/panel/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
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
    
    try:
        folios_data = supabase.table("folios_registrados").select("*").execute()
        folios = folios_data.data or []
        
        hoy = datetime.now(ZoneInfo(TZ)).date()
        for f in folios:
            try:
                if f.get('fecha_vencimiento'):
                    fecha_ven = datetime.fromisoformat(f['fecha_vencimiento']).date()
                    f['estado_calc'] = "VIGENTE" if hoy <= fecha_ven else "VENCIDO"
                else:
                    f['estado_calc'] = "SIN FECHA"
            except:
                f['estado_calc'] = "ERROR"
        
        print(f"[ADMIN_FOLIOS] Total: {len(folios)}")
        
    except Exception as e:
        print(f"[ADMIN_FOLIOS] Error: {e}")
        traceback.print_exc()
        folios = []
    
    return templates.TemplateResponse("admin_folios.html", {
        "request": request,
        "folios": folios
    })

@app.get("/panel/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/panel/login", status_code=303)

@app.get("/estado_folio/{folio}", response_class=HTMLResponse)
async def estado_folio(folio: str, request: Request):
    """Ruta QR - USA TU TEMPLATE ORIGINAL"""
    try:
        folio_limpio = ''.join(c for c in folio if c.isalnum())
        print(f"[CONSULTA] Buscando: {folio_limpio}")

        res = supabase.table("folios_registrados") \
            .select("*") \
            .eq("folio", folio_limpio) \
            .execute()

        if not res.data or len(res.data) == 0:
            print(f"[CONSULTA] NO ENCONTRADO: {folio_limpio}")
            
            return templates.TemplateResponse("resultado_consulta.html", {
                "request": request,
                "folio": str(folio_limpio),
                "vigente": False,
                "no_encontrado": True,
                "marca": "",
                "linea": "",
                "anio": "",
                "serie": "",
                "motor": "",
                "color": "",
                "nombre": "",
                "expedicion": "",
                "vencimiento": ""
            })

        row = res.data[0]
        print(f"[CONSULTA] ENCONTRADO: {folio_limpio}")

        try:
            fecha_exp_dt = datetime.fromisoformat(str(row['fecha_expedicion']))
            fecha_ven_dt = datetime.fromisoformat(str(row['fecha_vencimiento']))
        except Exception as e:
            print(f"[CONSULTA] Error fechas: {e}")
            return HTMLResponse("<h1>Error en fechas</h1>", status_code=500)

        hoy = datetime.now(ZoneInfo(TZ)).date()
        vigente = hoy <= fecha_ven_dt.date()

        return templates.TemplateResponse("resultado_consulta.html", {
            "request": request,
            "folio": str(folio_limpio),
            "vigente": bool(vigente),
            "no_encontrado": False,
            "marca": str(row.get('marca', '')),
            "linea": str(row.get('linea', '')),
            "anio": str(row.get('anio', '')),
            "serie": str(row.get('numero_serie', '')),
            "motor": str(row.get('numero_motor', '')),
            "color": str(row.get('color', '')),
            "nombre": str(row.get('contribuyente', '')),
            "expedicion": str(fecha_exp_dt.strftime('%d/%m/%Y')),
            "vencimiento": str(fecha_ven_dt.strftime('%d/%m/%Y'))
        })

    except Exception as e:
        print(f"[CONSULTA] CRÍTICO: {e}")
        traceback.print_exc()
        
        return HTMLResponse(f"<h1>Error: {str(e)}</h1>", status_code=500)

@app.get("/health")
async def health_check():
    try:
        bot_info = await bot.get_me()
        
        return {
            "status": "healthy",
            "timestamp": datetime.now(ZoneInfo(TZ)).isoformat(),
            "version": "6.3 - AGS FINAL",
            "bot": f"@{bot_info.username}" if bot_info else "error",
            "timers_activos": len(timers_activos)
        }
    except Exception as e:
        return {
            "status": "error", 
            "error": str(e)
        }

if __name__ == "__main__":
    import uvicorn
    print("[ARRANQUE] Sistema AGS Bot + Web")
    uvicorn.run(app, host="0.0.0.0", port=8000)
