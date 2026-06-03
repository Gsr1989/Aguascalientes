from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
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
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY", "")
BASE_URL         = "https://aguascalientes-gob-mx-ui-ciudadano.onrender.com"
OUTPUT_DIR       = "documentos"
PLANTILLA_PDF    = "DIGITAL_AGUASCALIENTES.pdf"
PLANTILLA_RECIBO = "Recibo-aguascalientes.pdf"
ENTIDAD          = "ags"
PRECIO_PERMISO   = 180
TZ               = os.getenv("TZ", "America/Mexico_City")

# Credenciales admin — mismas que CDMX y EDOMEX
ADMIN_USER = "Serg890105tm3"
ADMIN_PASS = "Serg890105tm3"

TEMPLATES_DIR = "templates"
STATIC_DIR    = "static"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR,    exist_ok=True)
os.makedirs(STATIC_DIR,    exist_ok=True)

templates  = Jinja2Templates(directory=TEMPLATES_DIR)
_jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ===================== CONSECUTIVOS =====================
CONSECUTIVOS_INICIALES = {
    "recibo_ingreso": 403202608800627,
    "pase_caja":      9000002373220,
    "numero_1":       93161700,
    "numero_2":       47101510
}

def obtener_siguiente_consecutivo(tipo: str) -> int:
    for intento in range(1000):
        try:
            resp = supabase.table("consecutivos_ags") \
                .select("valor").eq("tipo", tipo) \
                .order("valor", desc=True).limit(1).execute()
            siguiente = (int(resp.data[0]["valor"]) + 1) if resp.data else CONSECUTIVOS_INICIALES[tipo]
            supabase.table("consecutivos_ags").insert({
                "tipo": tipo, "valor": siguiente,
                "created_at": datetime.now(ZoneInfo(TZ)).isoformat()
            }).execute()
            print(f"[CONSECUTIVO] {tipo}: {siguiente} (intento {intento+1})")
            return siguiente
        except Exception as e:
            if "duplicate" in str(e).lower() or "unique" in str(e).lower():
                continue
            raise e
    return CONSECUTIVOS_INICIALES[tipo] + random.randint(1000, 9999)

# ===================== TIMERS 36H =====================
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        uid = timers_activos[folio]["user_id"] if folio in timers_activos else None
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        if uid:
            await bot.send_message(uid,
                f"⏰ TIEMPO AGOTADO - AGUASCALIENTES\n\n"
                f"El folio {folio} fue eliminado por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /chuleta")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos: return
        uid = timers_activos[folio]["user_id"]
        await bot.send_message(uid,
            f"⚡ RECORDATORIO - AGUASCALIENTES\n\n"
            f"Folio: {folio}\nTiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen).\n\n"
            f"📋 Para generar otro permiso use /chuleta")
    except Exception as e:
        print(f"Error recordatorio {folio}: {e}")

async def iniciar_timer_36h(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado folio {folio}, usuario {user_id} (36h)")
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            print(f"[TIMER] Expirado folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer 36h iniciado folio {folio}, total: {len(timers_activos)}")

def cancelar_timer_folio(folio: str) -> bool:
    if folio not in timers_activos: return False
    timers_activos[folio]["task"].cancel()
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]
    print(f"[SISTEMA] Timer cancelado folio {folio}")
    return True

def limpiar_timer_folio(folio: str):
    if folio not in timers_activos: return
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ===================== FOLIOS AGS — WATERMARK =================================
# Prefijo numérico "654" + número consecutivo.
# Watermark en tabla folio_watermark (prefijo = "AGS") — nunca retrocede.

FOLIO_PREFIJO_AGS   = "AGS"   # clave en folio_watermark
FOLIO_NUM_PREFIJO   = "654"   # prefijo visible en el folio
_folio_counter_ags  = {"siguiente": 1}
_folio_lock_ags     = asyncio.Lock()

def _sb_leer_watermark_ags() -> int | None:
    """Síncrono."""
    try:
        r = supabase.table("folio_watermark") \
            .select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO_AGS).execute()
        if r.data:
            return r.data[0]["ultimo_asignado"]
        return None
    except Exception as e:
        print(f"[ERROR] leer_watermark AGS: {e}")
        return None

def _sb_guardar_watermark_ags(numero: int):
    """Solo avanza, nunca retrocede. Síncrono."""
    try:
        supabase.table("folio_watermark").upsert({
            "prefijo":         FOLIO_PREFIJO_AGS,
            "ultimo_asignado": numero
        }).execute()
        print(f"[WATERMARK AGS] Guardado: {FOLIO_NUM_PREFIJO}{numero}")
    except Exception as e:
        print(f"[ERROR] guardar_watermark AGS: {e}")

def _sb_inicializar_folio_ags():
    """
    Al arrancar:
    1) Lee watermark (máximo histórico real).
    2) Si no existe, busca el máximo en DB activa y crea el watermark.
    3) El contador NUNCA baja aunque se borren folios expirados.
    Síncrono — llamar con asyncio.to_thread.
    """
    watermark = _sb_leer_watermark_ags()
    if watermark is not None:
        _folio_counter_ags["siguiente"] = watermark + 1
        print(f"[FOLIO AGS] Desde watermark: {FOLIO_NUM_PREFIJO}{watermark} "
              f"-> siguiente: {_folio_counter_ags['siguiente']}")
        return

    # Primera vez — construye watermark desde DB activa
    try:
        resp = supabase.table("folios_registrados") \
            .select("folio").eq("entidad", ENTIDAD) \
            .like("folio", f"{FOLIO_NUM_PREFIJO}%").execute()
        numeros = []
        for row in resp.data or []:
            f = row.get("folio", "")
            if isinstance(f, str) and f.startswith(FOLIO_NUM_PREFIJO):
                sufijo = f[len(FOLIO_NUM_PREFIJO):]
                if sufijo.isdigit():
                    numeros.append(int(sufijo))
        if numeros:
            maximo = max(numeros)
            _folio_counter_ags["siguiente"] = maximo + 1
            _sb_guardar_watermark_ags(maximo)
            print(f"[FOLIO AGS] Desde DB (primera vez): {FOLIO_NUM_PREFIJO}{maximo} "
                  f"-> siguiente: {_folio_counter_ags['siguiente']}")
        else:
            _folio_counter_ags["siguiente"] = 1
            print(f"[FOLIO AGS] Sin folios previos, empezando desde {FOLIO_NUM_PREFIJO}1")
    except Exception as e:
        print(f"[ERROR] inicializar_folio AGS: {e}")
        _folio_counter_ags["siguiente"] = 1

def _sb_folio_existe_ags(folio: str) -> bool:
    """Síncrono."""
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except Exception as e:
        print(f"[ERROR] verificar folio {folio}: {e}")
        return False

def _generar_folio_ags_sync() -> str:
    """
    Síncrono — usar con asyncio.to_thread + _folio_lock_ags.
    Busca SIEMPRE hacia arriba desde el último asignado.
    Nunca retrocede.
    """
    candidato = _folio_counter_ags["siguiente"]
    for _ in range(100_000):
        folio = f"{FOLIO_NUM_PREFIJO}{candidato}"
        if not _sb_folio_existe_ags(folio):
            _folio_counter_ags["siguiente"] = candidato + 1
            _sb_guardar_watermark_ags(candidato)
            print(f"[FOLIO AGS] Asignado: {folio} (siguiente: {_folio_counter_ags['siguiente']})")
            return folio
        print(f"[FOLIO AGS] {folio} ocupado -> probando siguiente")
        candidato += 1
    # Fallback extremo
    return f"{FOLIO_NUM_PREFIJO}{random.randint(50000, 99999)}"

async def _generar_folio_ags_async() -> str:
    """Async con Lock — sin race conditions."""
    async with _folio_lock_ags:
        return await asyncio.to_thread(_generar_folio_ags_sync)

# Mantener función síncrona para compatibilidad con rutas web
def generar_folio_ags() -> str:
    return _generar_folio_ags_sync()

# ===================== AUXILIARES =====================
def limpiar_entrada(texto: str) -> str:
    if not texto: return ""
    return ''.join(c for c in texto if c.isalnum() or c.isspace() or c in '-_./').strip().upper()

def formatear_folio_completo(folio: str) -> str:
    return f"AGS  / {folio} / {datetime.now().year}"

def generar_qr_simple_ags(folio):
    try:
        url = f"{BASE_URL}/estado_folio/{folio}"
        qr  = qrcode.QRCode(version=None,
                            error_correction=qrcode.constants.ERROR_CORRECT_M,
                            box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception as e:
        print(f"[QR] Error: {e}"); return None

def generar_pdf_unificado_ags(datos: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{datos['folio']}_ags.pdf")

    try:
        recibo_ingreso = obtener_siguiente_consecutivo("recibo_ingreso")
        pase_caja      = obtener_siguiente_consecutivo("pase_caja")
        numero_1       = obtener_siguiente_consecutivo("numero_1")
        numero_2       = obtener_siguiente_consecutivo("numero_2")

        serie_completa   = datos["serie"]
        ultimos_4_serie  = serie_completa[-4:] if len(serie_completa) >= 4 else serie_completa
        fecha_hora_dt    = datos['fecha_exp_dt']
        hora_formateada  = fecha_hora_dt.strftime("%I:%M %p").lower() \
                           .replace("am", "a. m.").replace("pm", "p. m.")
        fecha_hora_completa = f"{fecha_hora_dt.strftime('%d/%m/%Y')} {hora_formateada}"
        rfc_generico        = "XAXX010101000"

        MESES_MAYUS = ["ENE","FEB","MAR","ABR","MAY","JUN","JUL","AGO","SEP","OCT","NOV","DIC"]
        def fecha_espaciada(dt: datetime) -> str:
            return f"{dt.day:02d}   /   {MESES_MAYUS[dt.month-1]}   /   {dt.year}"

        # ── Página 1: permiso ──
        if os.path.exists(PLANTILLA_PDF):
            doc_permiso = fitz.open(PLANTILLA_PDF)
            pg_permiso  = doc_permiso[0]

            coords_ags = {
                "marca":          (245, 305, 25, (0,0,0)),
                "anio":           (245, 353, 25, (0,0,0)),
                "color":          (245, 402, 25, (0,0,0)),
                "serie":          (245, 450, 25, (0,0,0)),
                "motor":          (245, 498, 25, (0,0,0)),
                "fecha_exp_larga":(350, 543, 25, (0,0,0)),
                "fecha_ven_larga":(850, 543, 25, (0,0,0)),
            }

            # Folio formateado en rojo
            pg_permiso.insert_text(
                (828, 103),
                f"AGS  / {datos['folio']} / {datetime.now().year}",
                fontsize=30, color=(1,0,0)
            )

            pg_permiso.insert_text(coords_ags["marca"][:2],
                f"{datos['marca']}   {datos['linea']}",
                fontsize=coords_ags["marca"][2], color=coords_ags["marca"][3])

            for campo in ["anio","color","serie","motor"]:
                x,y,s,col = coords_ags[campo]
                pg_permiso.insert_text((x,y), str(datos[campo]), fontsize=s, color=col)

            pg_permiso.insert_text(coords_ags["fecha_exp_larga"][:2],
                fecha_espaciada(datos['fecha_exp_dt']),
                fontsize=coords_ags["fecha_exp_larga"][2],
                color=coords_ags["fecha_exp_larga"][3])
            pg_permiso.insert_text(coords_ags["fecha_ven_larga"][:2],
                fecha_espaciada(datos['fecha_ven_dt']),
                fontsize=coords_ags["fecha_ven_larga"][2],
                color=coords_ags["fecha_ven_larga"][3])

            img_qr = generar_qr_simple_ags(datos["folio"])
            if img_qr:
                buf = BytesIO(); img_qr.save(buf, format="PNG"); buf.seek(0)
                qr_pix = fitz.Pixmap(buf.read())
                pg_permiso.insert_image(fitz.Rect(975, 130, 975+138, 130+138),
                                        pixmap=qr_pix, overlay=True)
        else:
            doc_permiso = fitz.open()
            doc_permiso.new_page(width=595, height=842).insert_text(
                (50,50), "PERMISO AGS (Plantilla no encontrada)", fontsize=20)

        # ── Página 2: recibo ──
        if os.path.exists(PLANTILLA_RECIBO):
            doc_recibo = fitz.open(PLANTILLA_RECIBO)
            pg_recibo  = doc_recibo[0]

            coords_recibo = {
                "recibo_ingreso_1": (469, 62,  10, (0,0,0)),
                "recibo_ingreso_2": (462, 771,  8, (0,0,0)),
                "serie_folio":      (469, 70,   7, (0,0,0)),
                "pase_caja":        (469, 83,   8, (0,0,0)),
                "fecha_hora":       (469, 93,   7, (0,0,0)),
                "rfc":              ( 70, 165,  8, (0,0,0)),
                "nombre":           ( 70, 178,  8, (0,0,0)),
                "numero_1":         (149, 291,  5, (0,0,0)),
                "numero_2":         (190, 291,  5, (0,0,0)),
            }

            pg_recibo.insert_text(coords_recibo["recibo_ingreso_1"][:2], str(recibo_ingreso),
                fontsize=coords_recibo["recibo_ingreso_1"][2],
                color=coords_recibo["recibo_ingreso_1"][3], fontname="hebo")
            pg_recibo.insert_text(coords_recibo["recibo_ingreso_2"][:2], str(recibo_ingreso),
                fontsize=coords_recibo["recibo_ingreso_2"][2],
                color=coords_recibo["recibo_ingreso_2"][3])
            pg_recibo.insert_text(coords_recibo["serie_folio"][:2],
                f"{ultimos_4_serie}  {datos['folio']}",
                fontsize=coords_recibo["serie_folio"][2], color=coords_recibo["serie_folio"][3])
            pg_recibo.insert_text(coords_recibo["pase_caja"][:2], str(pase_caja),
                fontsize=coords_recibo["pase_caja"][2], color=coords_recibo["pase_caja"][3])
            pg_recibo.insert_text(coords_recibo["fecha_hora"][:2], fecha_hora_completa,
                fontsize=coords_recibo["fecha_hora"][2], color=coords_recibo["fecha_hora"][3])
            pg_recibo.insert_text(coords_recibo["rfc"][:2], rfc_generico,
                fontsize=coords_recibo["rfc"][2], color=coords_recibo["rfc"][3])
            pg_recibo.insert_text(coords_recibo["nombre"][:2], datos["nombre"],
                fontsize=coords_recibo["nombre"][2], color=coords_recibo["nombre"][3])
            pg_recibo.insert_text(coords_recibo["numero_1"][:2], str(numero_1),
                fontsize=coords_recibo["numero_1"][2], color=coords_recibo["numero_1"][3])
            pg_recibo.insert_text(coords_recibo["numero_2"][:2], str(numero_2),
                fontsize=coords_recibo["numero_2"][2], color=coords_recibo["numero_2"][3])
        else:
            doc_recibo = fitz.open()
            doc_recibo.new_page(width=595, height=842).insert_text(
                (50,50), "RECIBO (Plantilla no encontrada)", fontsize=20)

        doc_final = fitz.open()
        doc_final.insert_pdf(doc_permiso)
        doc_final.insert_pdf(doc_recibo)
        doc_final.save(out)
        doc_final.close(); doc_permiso.close()
        if os.path.exists(PLANTILLA_RECIBO): doc_recibo.close()

        print(f"[PDF] ✅ Generado: {out}")
        return out

    except Exception as e:
        print(f"[PDF] Error crítico: {e}"); raise e

# ===================== FSM =====================
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    nombre = State()

# ===================== HANDLERS =====================

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ Sistema Digital de Permisos Aguascalientes\n\n"
        f"💰 Costo: ${PRECIO_PERMISO} MXN\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ Su folio será eliminado automáticamente si no realiza el pago a tiempo.\n\n"
        "📋 Use /chuleta para generar un permiso."
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    folios_activos = obtener_folios_usuario(message.from_user.id)

    if folios_activos:
        texto   = "📋 FOLIOS AGS ACTIVOS\n" + "─" * 28 + "\n\n"
        botones = []
        for f in folios_activos:
            if f in timers_activos:
                seg  = max(0, int(TOTAL_MINUTOS_TIMER * 60 -
                                  (datetime.now() - timers_activos[f]["start_time"]).total_seconds()))
                h, m = divmod(seg // 60, 60)
                texto += f"Folio: {formatear_folio_completo(f)}\n{h}h {m}min restantes\n\n"
            else:
                texto += f"Folio: {formatear_folio_completo(f)}\n(sin timer)\n\n"
            botones.append([
                InlineKeyboardButton(
                    text=f"⏹️ Detener timer {f}",
                    callback_data=f"detener_{f}"
                )
            ])
        await message.answer(
            texto.strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=botones)
        )
        await message.answer(
            f"Para NUEVO permiso escribe la MARCA del vehículo:\n\nCosto: ${PRECIO_PERMISO} | Plazo: 36h"
        )
    else:
        await message.answer(
            f"🚗 NUEVO PERMISO - AGUASCALIENTES\n\n"
            f"💰 Costo: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Plazo de pago: 36 horas\n\n"
            f"Paso 1/7: MARCA del vehículo:"
        )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=limpiar_entrada(message.text))
    await message.answer("Paso 2/7: LÍNEA/MODELO:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=limpiar_entrada(message.text))
    await message.answer("Paso 3/7: AÑO (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Año inválido. Usa 4 dígitos (ej. 2021):"); return
    await state.update_data(anio=anio)
    await message.answer("Paso 4/7: NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=limpiar_entrada(message.text))
    await message.answer("Paso 5/7: NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=limpiar_entrada(message.text))
    await message.answer("Paso 6/7: COLOR:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=limpiar_entrada(message.text))
    await message.answer("Paso 7/7: NOMBRE COMPLETO del titular:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos          = await state.get_data()
    datos["nombre"] = limpiar_entrada(message.text)

    # Folio con watermark — async, nunca retrocede
    datos["folio"] = await _generar_folio_ags_async()

    tz  = ZoneInfo(TZ)
    hoy = datetime.now(tz)
    ven = hoy + timedelta(days=30)
    datos["fecha_exp"]    = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"]    = ven.strftime("%d/%m/%Y")
    datos["fecha_exp_dt"] = hoy
    datos["fecha_ven_dt"] = ven

    folio_fmt = formatear_folio_completo(datos["folio"])
    await message.answer(
        f"🔄 Generando permiso...\n"
        f"📄 Folio: {folio_fmt}\n"
        f"👤 Titular: {datos['nombre']}"
    )

    try:
        pdf_path = generar_pdf_unificado_ags(datos)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{datos['folio']}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{datos['folio']}")
        ]])

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=(
                f"📄 PERMISO + RECIBO — AGUASCALIENTES\n"
                f"Folio: {folio_fmt}\n"
                f"Expedición: {datos['fecha_exp']}\n"
                f"Vencimiento: {datos['fecha_ven']}\n\n"
                f"⏰ TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        supabase.table("folios_registrados").insert({
            "folio":             datos["folio"],
            "marca":             datos["marca"],
            "linea":             datos["linea"],
            "anio":              datos["anio"],
            "numero_serie":      datos["serie"],
            "numero_motor":      datos["motor"],
            "color":             datos["color"],
            "contribuyente":     datos["nombre"],
            "fecha_expedicion":  hoy.date().isoformat(),
            "fecha_vencimiento": ven.date().isoformat(),
            "entidad":           ENTIDAD,
            "estado":            "PENDIENTE",
            "user_id":           message.from_user.id,
            "username":          message.from_user.username or "Sin username"
        }).execute()

        await iniciar_timer_36h(message.from_user.id, datos["folio"])

        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio_fmt}\n"
            f"💵 Monto: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            f"📸 Envíe la foto de su comprobante aquí mismo.\n"
            f"⚠️ Sin pago en 36h el folio se elimina automáticamente.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await message.answer(f"❌ ERROR: {e}\n\n📋 Para generar otro permiso use /chuleta")
    finally:
        await state.clear()

# ===================== CALLBACKS =====================

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if not folio.startswith("654"):
        await callback.answer("❌ Folio inválido", show_alert=True); return
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN", "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error BD validar {folio}: {e}")
        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO — AGUASCALIENTES\n"
                f"📄 Folio: {formatear_folio_completo(folio)}\n"
                f"Tu permiso está activo.\n\n📋 Para generar otro permiso use /chuleta")
        except Exception as e:
            print(f"Error notificando usuario: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        try:
            supabase.table("folios_registrados").update({
                "estado": "TIMER_DETENIDO", "fecha_detencion": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error BD detener {folio}: {e}")
        await callback.answer("⏹️ Timer detenido", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\n📄 Folio: {formatear_folio_completo(folio)}\n\n"
            f"El folio ya NO se eliminará automáticamente.\n\n"
            f"📋 Para generar otro permiso use /chuleta")
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

# ===================== ADMIN SERO =====================

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    if not folio or not folio.startswith("654"):
        await message.answer(
            "⚠️ Formato: SERO654X (folio debe iniciar con 654).\n\n"
            "📋 Para generar otro permiso use /chuleta"); return
    cancelado = cancelar_timer_folio(folio)
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "VALIDADO_ADMIN", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute()
    folio_fmt = formatear_folio_completo(folio)
    if cancelado:
        await message.answer(
            f"✅ Validación admin exitosa\n📄 Folio: {folio_fmt}\n⏹️ Timer detenido\n\n"
            f"📋 Para generar otro permiso use /chuleta")
    else:
        await message.answer(
            f"✅ Validación admin\n📄 Folio: {folio_fmt}\n⚠️ Timer ya estaba inactivo\n\n"
            f"📋 Para generar otro permiso use /chuleta")

# ===================== COMPROBANTE FOTO =====================

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer(
            "ℹ️ No tienes folios pendientes.\n\n📋 Para generar otro permiso use /chuleta"); return
    if len(folios) > 1:
        lista = "\n".join(f"• {formatear_folio_completo(f)}" for f in folios)
        pending_comprobantes[uid] = "waiting_folio"
        await message.answer(
            f"📄 Varios folios activos:\n\n{lista}\n\n"
            f"Responde con el NÚMERO DE FOLIO para este comprobante.\n\n"
            f"📋 Para generar otro permiso use /chuleta"); return
    folio = folios[0]; cancelar_timer_folio(folio)
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute()
    await message.answer(
        f"✅ Comprobante recibido\n📄 Folio: {formatear_folio_completo(folio)}\n"
        f"⏹️ Timer detenido.\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(lambda m: m.from_user.id in pending_comprobantes
            and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    uid = message.from_user.id
    fe  = message.text.strip().upper()
    fl  = obtener_folios_usuario(uid)
    if fe not in fl:
        await message.answer(
            "❌ Folio no en tu lista.\n\n📋 Para generar otro permiso use /chuleta"); return
    cancelar_timer_folio(fe); del pending_comprobantes[uid]
    with suppress(Exception):
        supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", fe).execute()
    await message.answer(
        f"✅ Comprobante asociado.\n📄 Folio: {formatear_folio_completo(fe)}\n\n"
        f"📋 Para generar otro permiso use /chuleta")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer(
            "ℹ️ No hay folios activos.\n\n📋 Para generar otro permiso use /chuleta"); return
    lista   = []
    botones = []
    for f in folios:
        if f in timers_activos:
            seg  = max(0, int(TOTAL_MINUTOS_TIMER * 60 -
                               (datetime.now() - timers_activos[f]["start_time"]).total_seconds()))
            h, m = divmod(seg // 60, 60)
            lista.append(f"• {formatear_folio_completo(f)} ({h}h {m}min)")
        else:
            lista.append(f"• {formatear_folio_completo(f)} (sin timer)")
        botones.append([InlineKeyboardButton(
            text=f"⏹️ Detener {f}", callback_data=f"detener_{f}"
        )])
    await message.answer(
        f"📋 FOLIOS AGS ACTIVOS ({len(folios)})\n\n" + "\n".join(lista) +
        "\n\n⏰ Timer 36h por folio.\n📸 Envía imagen para comprobante.\n\n"
        "📋 Para generar otro permiso use /chuleta",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones)
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Aguascalientes.")

# ===================== FASTAPI =====================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema AGS activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    # Inicializa contador desde watermark al arrancar
    await asyncio.to_thread(_sb_inicializar_folio_ags)
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
    _keep_task = asyncio.create_task(keep_alive())
    print(f"[WEBHOOK] {webhook_url}")
    print(f"[SISTEMA] AGS v7.0 listo — "
          f"siguiente folio: {FOLIO_NUM_PREFIJO}{_folio_counter_ags['siguiente']}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError): await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Bot Permisos AGS", version="7.0")
app.add_middleware(SessionMiddleware, secret_key="tu_clave_secreta_super_segura_123456")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ===================== RUTAS WEB =====================

@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse(request, "login.html")

@app.post("/panel/login")
async def login_post(request: Request,
                     username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["admin"]    = True
        request.session["username"] = username
        return RedirectResponse(url="/panel/admin", status_code=303)
    return RedirectResponse(url="/panel/login?error=1", status_code=303)

@app.get("/panel/admin", response_class=HTMLResponse)
async def panel_admin(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    return templates.TemplateResponse(request, "panel.html", {
        "timers_activos":       len(timers_activos),
        "usuarios_con_folios":  len(user_folios)
    })

@app.get("/panel/admin_folios", response_class=HTMLResponse)
async def admin_folios_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        folios = supabase.table("folios_registrados").select("*").execute().data or []
        hoy    = datetime.now(ZoneInfo(TZ)).date()
        for f in folios:
            try:
                fv = datetime.fromisoformat(f['fecha_vencimiento']).date()
                f['estado_calc'] = "VIGENTE" if hoy <= fv else "VENCIDO"
            except:
                f['estado_calc'] = "ERROR"
    except Exception as e:
        print(f"[ADMIN_FOLIOS] Error: {e}"); folios = []
    return templates.TemplateResponse(request, "admin_folios.html", {"folios": folios})

@app.get("/panel/admin_tablas", response_class=HTMLResponse)
async def admin_tablas_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    tablas = {'folios_registrados': 'Folios Registrados', 'consecutivos_ags': 'Consecutivos AGS'}
    return templates.TemplateResponse(request, "admin_tablas.html", {"tablas": tablas})

@app.get("/panel/admin_tabla/{tabla}", response_class=HTMLResponse)
async def admin_tabla_detalle(request: Request, tabla: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        registros = supabase.table(tabla).select("*").execute().data or []
    except:
        registros = []
    return templates.TemplateResponse(request, "admin_tabla_detalle.html",
                                      {"tabla": tabla, "registros": registros})

@app.get("/panel/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/panel/login", status_code=303)

@app.get("/panel/editar_folio/{folio}", response_class=HTMLResponse)
async def editar_folio_get(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        res = supabase.table("folios_registrados").select("*").eq("folio", folio).limit(1).execute()
        registro = (res.data or [None])[0]
        if not registro:
            return RedirectResponse(url="/panel/admin_folios?error=not_found", status_code=303)
        return templates.TemplateResponse(request, "editar_folio.html", {"registro": registro})
    except Exception as e:
        print(f"Error obteniendo folio: {e}")
        return RedirectResponse(url="/panel/admin_folios?error=1", status_code=303)

@app.post("/panel/editar_folio/{folio}")
async def editar_folio_post(request: Request, folio: str,
    marca: str = Form(...), linea: str = Form(...), anio: str = Form(...),
    numero_serie: str = Form(...), numero_motor: str = Form(...),
    color: str = Form(...), contribuyente: str = Form(...),
    fecha_expedicion: str = Form(...), fecha_vencimiento: str = Form(...),
    estado: str = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        supabase.table("folios_registrados").update({
            "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "numero_serie": numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color": color.upper(), "contribuyente": contribuyente.upper(),
            "fecha_expedicion": fecha_expedicion, "fecha_vencimiento": fecha_vencimiento,
            "estado": estado
        }).eq("folio", folio).execute()
        return RedirectResponse(url="/panel/admin_folios?success=1", status_code=303)
    except Exception as e:
        print(f"Error actualizando folio: {e}")
        return RedirectResponse(url=f"/panel/editar_folio/{folio}?error=1", status_code=303)

@app.post("/panel/eliminar_folio/{folio}")
async def eliminar_folio_web(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        cancelar_timer_folio(folio)
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        return RedirectResponse(url="/panel/admin_folios?deleted=1", status_code=303)
    except Exception as e:
        print(f"Error eliminando folio: {e}")
        return RedirectResponse(url="/panel/admin_folios?error=delete", status_code=303)

@app.get("/panel/registro_admin", response_class=HTMLResponse)
async def registro_admin_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    return templates.TemplateResponse(request, "registro_admin.html")

@app.post("/panel/registro_admin")
async def registro_admin_post(request: Request,
    folio: str = Form(None), marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), numero_serie: str = Form(...), numero_motor: str = Form(...),
    color: str = Form(...), contribuyente: str = Form(...),
    fecha_expedicion: str = Form(None), fecha_vencimiento: str = Form(None)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        tz = ZoneInfo(TZ)
        folio_generado = folio.strip() if folio and folio.strip() else generar_folio_ags()
        fecha_exp = datetime.fromisoformat(fecha_expedicion).date() \
            if fecha_expedicion and fecha_expedicion.strip() else datetime.now(tz).date()
        fecha_ven = datetime.fromisoformat(fecha_vencimiento).date() \
            if fecha_vencimiento and fecha_vencimiento.strip() else fecha_exp + timedelta(days=30)

        datos_pdf = {
            "folio":       folio_generado,
            "marca":       marca.upper(), "linea":  linea.upper(), "anio":  anio,
            "serie":       numero_serie.upper(), "motor": numero_motor.upper(),
            "color":       color.upper(), "nombre": contribuyente.upper(),
            "fecha_exp":   fecha_exp.strftime("%d/%m/%Y"),
            "fecha_ven":   fecha_ven.strftime("%d/%m/%Y"),
            "fecha_exp_dt":datetime.combine(fecha_exp, datetime.min.time()).replace(tzinfo=tz),
            "fecha_ven_dt":datetime.combine(fecha_ven, datetime.min.time()).replace(tzinfo=tz)
        }
        generar_pdf_unificado_ags(datos_pdf)
        supabase.table("folios_registrados").insert({
            "folio":             folio_generado,
            "marca":             marca.upper(), "linea": linea.upper(), "anio": anio,
            "numero_serie":      numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color":             color.upper(), "contribuyente": contribuyente.upper(),
            "fecha_expedicion":  fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "entidad":           ENTIDAD, "estado": "VALIDADO_ADMIN",
            "creado_por":        request.session.get("username", "admin")
        }).execute()
        return RedirectResponse(
            url=f"/panel/admin_folios?success=created&folio={folio_generado}", status_code=303)
    except Exception as e:
        print(f"Error en registro admin: {e}")
        return RedirectResponse(url="/panel/registro_admin?error=1", status_code=303)

# ===================== WEBHOOK =====================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        await dp.feed_webhook_update(bot, types.Update(**data))
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}"); return {"ok": False, "error": str(e)}

@app.get("/estado_folio/{folio}", response_class=HTMLResponse)
async def estado_folio(folio: str, request: Request):
    try:
        folio_limpio = ''.join(c for c in folio if c.isalnum())
        res = supabase.table("folios_registrados").select("*").eq("folio", folio_limpio).limit(1).execute()
        row = (res.data or [None])[0]
        tmpl = _jinja_env.get_template("resultado_consulta.html")
        if not row:
            return HTMLResponse(tmpl.render(
                folio=folio_limpio, vigente=False, no_encontrado=True,
                marca="", linea="", anio="", serie="", motor="",
                color="", nombre="", expedicion="", vencimiento=""))
        hoy       = datetime.now(ZoneInfo(TZ)).date()
        fecha_ven = datetime.fromisoformat(row['fecha_vencimiento']).date()
        return HTMLResponse(tmpl.render(
            folio=folio_limpio, vigente=hoy <= fecha_ven, no_encontrado=False,
            marca=row.get('marca',''), linea=row.get('linea',''), anio=row.get('anio',''),
            serie=row.get('numero_serie',''), motor=row.get('numero_motor',''),
            color=row.get('color',''), nombre=row.get('contribuyente',''),
            expedicion=datetime.fromisoformat(row['fecha_expedicion']).strftime('%d/%m/%Y'),
            vencimiento=fecha_ven.strftime('%d/%m/%Y')))
    except Exception as e:
        return HTMLResponse(f"<h1>Error</h1><p>{e}</p>", status_code=500)

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(f"""<!DOCTYPE html><html><head>
<title>Sistema Permisos AGS</title><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:Arial,sans-serif;margin:0;padding:40px;
     background:linear-gradient(135deg,#1a237e,#283593);
     min-height:100vh;display:flex;align-items:center;justify-content:center}}
.c{{max-width:580px;width:100%;background:#fff;padding:40px;
    border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.3);text-align:center}}
h1{{color:#1a237e;font-size:26px}} h2{{color:#283593;font-size:20px;font-weight:normal}}
.badge{{display:inline-block;background:#e8f5e8;color:#2d5730;border:2px solid #4caf50;
        padding:6px 16px;border-radius:20px;font-size:13px;font-weight:bold;margin-bottom:25px}}
.info{{background:#f5f5f5;padding:20px;border-radius:10px;margin:20px 0;text-align:left}}
.btn{{display:inline-block;background:#1a237e;color:#fff;padding:14px 32px;
      border-radius:8px;text-decoration:none;font-weight:bold;margin-top:20px}}
</style></head><body><div class="c">
<h1>🏛️ Sistema Digital de Permisos</h1>
<h2>Estado de Aguascalientes</h2>
<div class="badge">✅ Sistema Operativo</div>
<div class="info"><ul style="padding-left:20px">
<li><strong>Versión:</strong> 7.0 — Watermark + /chuleta mejorado</li>
<li><strong>Costo:</strong> ${PRECIO_PERMISO} MXN</li>
<li><strong>Tiempo límite:</strong> 36 horas</li>
<li><strong>Timers activos:</strong> {len(timers_activos)}</li>
<li><strong>Siguiente folio:</strong> {FOLIO_NUM_PREFIJO}{_folio_counter_ags['siguiente']}</li>
</ul></div>
<a href="/panel/login" class="btn">→ Panel de Administración</a>
</div></body></html>""")

@app.get("/health")
async def health_check():
    try:
        supabase.table("folios_registrados").select("count", count="exact").limit(1).execute()
        bot_info = await bot.get_me()
        return {
            "status": "healthy", "version": "7.0",
            "timestamp": datetime.now(ZoneInfo(TZ)).isoformat(),
            "services": {
                "database":           "conectado",
                "telegram_bot":       f"@{bot_info.username}",
                "timers_activos":     len(timers_activos),
                "siguiente_folio":    f"{FOLIO_NUM_PREFIJO}{_folio_counter_ags['siguiente']}",
            }
        }
    except Exception as e:
        return {"status": "error", "error": str(e),
                "timestamp": datetime.now(ZoneInfo(TZ)).isoformat()}

if __name__ == "__main__":
    import uvicorn
    print(f"[SISTEMA] AGS v7.0 iniciando...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
