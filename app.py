from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
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

# ===================== CONFIG AGUASCALIENTES =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # para URL de verificación en QR
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "DIGITAL_AGUASCALIENTES.pdf"
ENTIDAD = "ags"
PRECIO_PERMISO = 180
TZ = os.getenv("TZ", "America/Mexico_City")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== SUPABASE =====================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===================== BOT =====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== TIMERS (12 HORAS) =====================
# timers_activos: {folio: {"task": task, "user_id": int, "start_time": datetime}}
timers_activos = {}
# user_folios: {user_id: [folios]}
user_folios = {}

async def eliminar_folio_automatico(folio: str):
    """Borra definitivamente el folio de Supabase y limpia timer."""
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        # Borrar en BD
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        # Avisar al usuario (si existe)
        if user_id:
            with suppress(Exception):
                await bot.send_message(
                    user_id,
                    f"⏰ **TIEMPO AGOTADO**\n\nEl folio **{folio}** fue eliminado por no recibir comprobante ni validación admin en 12 horas.",
                    parse_mode="Markdown"
                )
    except Exception as e:
        print(f"[TIMER] Error al eliminar folio {folio}: {e}")
    finally:
        limpiar_timer_folio(folio)

async def iniciar_timer_12h(user_id: int, folio: str):
    """Inicia timer exacto de 12 horas; si vence y nadie envió foto ni SERO<folio>, se borra de Supabase."""
    async def timer_task():
        try:
            await asyncio.sleep(12 * 60 * 60)  # 12 horas
            # Si sigue activo, borra
            if folio in timers_activos:
                await eliminar_folio_automatico(folio)
        except asyncio.CancelledError:
            # Timer cancelado por comprobante o admin
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

# ===================== COORDENADAS (AJUSTA A TU PDF) =====================
# (x, y, fontsize, color_rgb). Se imprimen en NEGRITAS (fontname helvb).
coords_ags = {
    "folio": (520, 120, 14, (1, 0, 0)),      # ROJO
    "marca": (120, 200, 11, (0, 0, 0)),
    "modelo": (120, 220, 11, (0, 0, 0)),     # "modelo" imprime VALOR de línea
    "color": (120, 240, 11, (0, 0, 0)),
    "serie": (120, 260, 11, (0, 0, 0)),
    "motor": (120, 280, 11, (0, 0, 0)),
    "nombre": (120, 300, 11, (0, 0, 0)),
    "fecha_ven_larga": (120, 320, 11, (0, 0, 0)),  # "xx mes xxxx"
}

# ===================== FECHAS =====================
ABR_MES = ["ene","feb","mar","abr","May","Jun","jul","ago","sep","oct","nov","dic"]  # exacto como pediste
def fecha_larga(dt: datetime) -> str:
    return f"{dt.day:02d} {ABR_MES[dt.month-1]} {dt.year}"

# ===================== FOLIO PREFIJO 129 =====================
def generar_folio_ags():
    """Prefijo fijo '129' + incremental pegado: 1292, 1293, ... 12910, ..."""
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

        siguiente = (max(usados) + 1) if usados else 2  # arranca en 1292
        while f"{prefijo}{siguiente}" in existentes:
            siguiente += 1
        return f"{prefijo}{siguiente}"
    except Exception as e:
        print(f"[FOLIO] Error: {e}")
        return f"{prefijo}{random.randint(10000,99999)}"

# ===================== QR DINÁMICO (texto + URL) =====================
def generar_qr_dinamico_ags(datos):
    """
    QR con payload de TEXTO legible + URL de verificación al final.
    """
    try:
        url_consulta = f"{BASE_URL}/consulta_folio/{datos['folio']}" if BASE_URL else f"https://example.com/consulta_folio/{datos['folio']}"
        payload = (
            f"Marca: {datos['marca']}\n"
            f"Línea: {datos['linea']}\n"
            f"Año: {datos['anio']}\n"
            f"Serie: {datos['serie']}\n"
            f"Motor: {datos['motor']}\n"
            f"Color: {datos['color']}\n"
            f"Nombre: {datos['nombre']}\n"
            f"Expedición: {datos['fecha_exp']}\n"
            f"Vencimiento: {datos['fecha_ven']}\n"
            f"Verificación: {url_consulta}"
        )
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(payload)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception as e:
        print(f"[QR] Error: {e}")
        return None

# ===================== PDF (valores en negritas, folio rojo) =====================
def generar_pdf_ags(datos: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{datos['folio']}_ags.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    def put(key, value):
        if key not in coords_ags:
            return
        x, y, s, col = coords_ags[key]
        pg.insert_text((x, y), str(value), fontsize=s, color=col, fontname="helvb")  # negritas

    # Folio (ROJO)
    put("folio", datos["folio"])
    # Valores en negritas (SIN etiquetas)
    put("marca", datos["marca"])
    put("modelo", datos["linea"])       # modelo = línea (solo valor)
    put("color", datos["color"])
    put("serie", datos["serie"])
    put("motor", datos["motor"])
    put("nombre", datos["nombre"])
    put("fecha_ven_larga", fecha_larga(datos["fecha_ven_dt"]))

    # QR
    img_qr = generar_qr_dinamico_ags(datos)
    if img_qr:
        buf = BytesIO()
        img_qr.save(buf, format="PNG")
        buf.seek(0)
        qr_pix = fitz.Pixmap(buf.read())
        rect = fitz.Rect(495, 40, 575, 120)  # AJUSTA A TU PDF
        pg.insert_image(rect, pixmap=qr_pix, overlay=True)

    doc.save(out)
    doc.close()
    return out

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
    await message.answer(
        "🏛️ **Sistema Digital de Permisos Aguascalientes**\n\n"
        f"💰 **Costo:** ${PRECIO_PERMISO} MXN\n"
        "⏰ **Tiempo límite:** 12 horas (si no envía comprobante o clave admin, se elimina)\n"
        "📋 Use /permiso para iniciar su trámite",
        parse_mode="Markdown"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    activos = obtener_folios_usuario(message.from_user.id)
    if activos:
        await message.answer(
            f"📋 **Folios activos:** {', '.join(activos)}\n"
            f"Cada folio expira si no envías comprobante en **12h**.\n\n"
            "**Paso 1/7:** Ingresa la **MARCA** del vehículo:",
            parse_mode="Markdown"
        )
    else:
        await message.answer("**Paso 1/7:** Ingresa la **MARCA** del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("**Paso 2/7:** Ingresa la **LÍNEA/MODELO**:", parse_mode="Markdown")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("**Paso 3/7:** Ingresa el **AÑO (4 dígitos)**:", parse_mode="Markdown")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ El año debe tener 4 dígitos. Intenta de nuevo:")
        return
    await state.update_data(anio=anio)
    await message.answer("**Paso 4/7:** Ingresa el **NÚMERO DE SERIE**:", parse_mode="Markdown")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("**Paso 5/7:** Ingresa el **NÚMERO DE MOTOR**:", parse_mode="Markdown")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("**Paso 6/7:** Ingresa el **COLOR**:", parse_mode="Markdown")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("**Paso 7/7:** Ingresa el **NOMBRE COMPLETO del titular**:", parse_mode="Markdown")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip().upper()
    datos["folio"] = generar_folio_ags()

    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz)
    ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = ven.strftime("%d/%m/%Y")
    datos["fecha_ven_dt"] = ven

    # Aviso de proceso
    await message.answer(
        "🔄 **Generando permiso...**\n\n"
        f"📄 **Folio:** {datos['folio']}\n"
        f"👤 **Titular:** {datos['nombre']}\n"
        "Se emitirá con QR dinámico (texto + URL).",
        parse_mode="Markdown"
    )

    try:
        # Generar PDF
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
            "fecha_ven_dt": datos["fecha_ven_dt"],
        })

        # Enviar PDF
        await message.answer_document(
            FSInputFile(pdf_path),
            caption=(
                "📄 **PERMISO DIGITAL – AGUASCALIENTES**\n"
                f"**Folio:** {datos['folio']}\n"
                f"**Vigencia:** 30 días\n"
                "🔳 QR con datos (texto) + URL de verificación"
            ),
            parse_mode="Markdown"
        )

        # Guardar en Supabase (estado PENDIENTE)
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

        # Opcional compatibilidad con borradores_registros
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

        # Iniciar timer de 12h
        await iniciar_timer_12h(message.from_user.id, datos["folio"])

        # Instrucciones de pago
        await message.answer(
            f"💰 **INSTRUCCIONES DE PAGO**\n\n"
            f"📄 **Folio:** {datos['folio']}\n"
            f"💵 **Monto:** ${PRECIO_PERMISO} MXN\n"
            f"⏰ **Tiempo límite:** 12 horas (si no envías comprobante, se elimina)\n\n"
            "📸 **IMPORTANTE:** Envía la **foto** de tu comprobante aquí mismo para detener el timer.\n"
            "🔑 **ADMIN:** Para validar manual, enviar **SERO<folio>** (ej. `SERO1292`).",
            parse_mode="Markdown"
        )

    except Exception as e:
        await message.answer(f"❌ **ERROR**: {e}\n\nIntenta de nuevo con /permiso", parse_mode="Markdown")
    finally:
        await state.clear()

# ===================== HANDLER: comprobante (FOTO) =====================
@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios = obtener_folios_usuario(user_id)
    if not folios:
        await message.answer("ℹ️ No tienes folios pendientes. Usa /permiso para iniciar uno nuevo.")
        return
    # Si tiene más de uno, tomamos el más reciente (último) por simplicidad
    folio = folios[-1]

    # Detener timer y conservar en Supabase
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

    await message.answer(
        f"✅ **Comprobante recibido**\n\n"
        f"📄 **Folio:** {folio}\n"
        f"⏹️ Timer detenido. Tu folio se conserva en el sistema mientras verificamos.",
        parse_mode="Markdown"
    )

# ===================== HANDLER: código admin SERO<folio> =====================
@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    if not folio or not folio.startswith("129"):
        await message.answer("⚠️ Formato: `SERO1292` (folio debe iniciar con 129).", parse_mode="Markdown")
        return

    # Detener timer y conservar en Supabase (VALIDADO_ADMIN)
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

    await message.answer(
        f"✅ **Validación admin exitosa**\n\n"
        f"📄 **Folio:** {folio}\n"
        f"⏹️ Timer detenido y folio preservado en Supabase.",
        parse_mode="Markdown"
    )

# ===================== HANDLER: costo =====================
@dp.message(lambda m: m.text and any(p in m.text.lower() for p in ["costo","precio","cuanto","cuánto","pago","monto","depósito","deposito"]))
async def responder_costo(message: types.Message):
    await message.answer(f"💰 **Costo del permiso:** ${PRECIO_PERMISO} MXN\nUsa /permiso para iniciar tu trámite.", parse_mode="Markdown")

# ===================== FALLBACK =====================
@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Aguascalientes. Usa /permiso para iniciar.", parse_mode="Markdown")

# ===================== FASTAPI + WEBHOOK =====================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url, allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
        print(f"[WEBHOOK] {webhook_url}")
    else:
        print("[WEBHOOK] BASE_URL vacío: modo polling si corres local.")
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
    <h3>Permisos AGS - Online</h3>
    <ul>
      <li>Timers activos: {len(timers_activos)}</li>
      <li>Entidad: {ENTIDAD}</li>
      <li>Plantilla: {PLANTILLA_PDF}</li>
    </ul>
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

# ===================== VERIFICACIÓN EN LÍNEA =====================
@app.get("/consulta_folio/{folio}", response_class=HTMLResponse)
async def consulta_folio(folio: str):
    try:
        res = supabase.table("folios_registrados").select("*").eq("folio", folio).limit(1).execute()
        row = (res.data or [None])[0]
        if not row:
            return HTMLResponse(f"<h3>Folio {folio} no encontrado o eliminado.</h3>", status_code=404)

        # Render simple
        html = f"""
        <html><body style='font-family: system-ui;'>
        <h2>Permiso Digital – Aguascalientes</h2>
        <p><b>Folio:</b> {row.get('folio','')}</p>
        <p><b>Marca:</b> {row.get('marca','')}</p>
        <p><b>Línea:</b> {row.get('linea','')}</p>
        <p><b>Año:</b> {row.get('anio','')}</p>
        <p><b>Serie:</b> {row.get('numero_serie','')}</p>
        <p><b>Motor:</b> {row.get('numero_motor','')}</p>
        <p><b>Color:</b> {row.get('color','')}</p>
        <p><b>Nombre:</b> {row.get('contribuyente','')}</p>
        <p><b>Expedición:</b> {row.get('fecha_expedicion','')}</p>
        <p><b>Vencimiento:</b> {row.get('fecha_vencimiento','')}</p>
        <p><b>Entidad:</b> {row.get('entidad','').upper()}</p>
        <p><b>Estado:</b> {row.get('estado','')}</p>
        </body></html>
        """
        return HTMLResponse(html, status_code=200)
    except Exception as e:
        return HTMLResponse(f"<h3>Error: {e}</h3>", status_code=500)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
