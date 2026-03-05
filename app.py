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
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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
PLANTILLA_RECIBO = "Recibo-aguascalientes.pdf"
ENTIDAD = "ags"
PRECIO_PERMISO = 180
TZ = os.getenv("TZ", "America/Mexico_City")

TEMPLATES_DIR = "templates"
STATIC_DIR = "static"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
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
    """Obtiene el siguiente consecutivo de Supabase con reintentos anti-duplicación"""
    max_intentos = 1000
    
    for intento in range(max_intentos):
        try:
            # Obtener el último consecutivo usado
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
            
            # Intentar insertar el nuevo consecutivo
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
    
    # Fallback después de todos los intentos
    print(f"[CONSECUTIVO] FALLBACK para {tipo} después de {max_intentos} intentos")
    return CONSECUTIVOS_INICIALES[tipo] + random.randint(1000, 9999)

# ===================== SISTEMA DE TIMERS - 36 HORAS =====================
timers_activos = {}
user_folios = {}

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después de 36 horas"""
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
    """Envía recordatorios de pago"""
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
    """Inicia el timer de 36 horas con recordatorios progresivos"""
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
    """Cancela el timer de un folio específico cuando el usuario paga"""
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
    """Limpia todas las referencias de un folio tras expirar"""
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
    """Genera folio único con prefijo 654 verificando duplicados (hasta 10000 intentos)"""
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
    """Genera el formato completo del folio: AGS  / (folio) / 2026"""
    año_actual = datetime.now().year
    return f"AGS  / {folio} / {año_actual}"

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
        
        fecha_exp = row.get('fecha_expedicion', '')
        if fecha_exp:
            try:
                fecha_exp_dt = datetime.fromisoformat(fecha_exp)
                fecha_exp = fecha_exp_dt.strftime("%d/%m/%Y")
            except:
                pass
        
        fecha_ven = row.get('fecha_vencimiento', '')
        if fecha_ven:
            try:
                fecha_ven_dt = datetime.fromisoformat(fecha_ven)
                fecha_ven = fecha_ven_dt.strftime("%d/%m/%Y")
            except:
                pass
        
        folio_completo = formatear_folio_completo(row.get('folio', ''))
        
        datos = {
            'folio': row.get('folio', ''),
            'folio_completo': folio_completo,
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

def generar_pdf_unificado_ags(datos: dict) -> str:
    """Genera PDF unificado: PERMISO (página 1) + RECIBO (página 2)"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{datos['folio']}_ags.pdf")
    
    try:
        # ===== GENERAR CONSECUTIVOS =====
        recibo_ingreso = obtener_siguiente_consecutivo("recibo_ingreso")
        pase_caja = obtener_siguiente_consecutivo("pase_caja")
        numero_1 = obtener_siguiente_consecutivo("numero_1")
        numero_2 = obtener_siguiente_consecutivo("numero_2")
        
        # ===== EXTRAER ÚLTIMOS 4 DÍGITOS DE SERIE =====
        serie_completa = datos["serie"]
        ultimos_4_serie = serie_completa[-4:] if len(serie_completa) >= 4 else serie_completa
        
        # ===== FORMATO FECHA/HORA =====
        fecha_hora_dt = datos['fecha_exp_dt']
        # Formato: 04/03/2026 10:20 p. m.
        hora_formateada = fecha_hora_dt.strftime("%I:%M %p").lower().replace("am", "a. m.").replace("pm", "p. m.")
        fecha_hora_completa = f"{fecha_hora_dt.strftime('%d/%m/%Y')} {hora_formateada}"
        
        # ===== RFC GENÉRICO =====
        rfc_generico = "XAXX010101000"
        
        folio_completo = formatear_folio_completo(datos["folio"])
        
        # ===== PÁGINA 1: PERMISO =====
        if os.path.exists(PLANTILLA_PDF):
            doc_permiso = fitz.open(PLANTILLA_PDF)
            pg_permiso = doc_permiso[0]

            coords_ags = {
                "folio": (835, 103, 30),
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
            
            # ========================================
            # 🔧 AJUSTES DEL QR - EDITA AQUÍ
            # ========================================
            try:
                img_qr = generar_qr_simple_ags(datos["folio"])
                if img_qr:
                    buf = BytesIO()
                    img_qr.save(buf, format="PNG")
                    buf.seek(0)
                    qr_pix = fitz.Pixmap(buf.read())
                    
                    # 📍 COORDENADAS DEL QR (EDITA ESTOS VALORES)
                    # qr_x: Mover IZQUIERDA (menor número) o DERECHA (mayor número)
                    # qr_y: Mover ARRIBA (menor número) o ABAJO (mayor número)
                    qr_x = 990  # ← Cambia este número para mover HORIZONTAL
                    qr_y = 137  # ← Cambia este número para mover VERTICAL (número menor = más arriba)
                    
                    # 📏 TAMAÑO DEL QR (EDITA ESTE VALOR)
                    # qr_width/qr_height: Número mayor = QR más grande, número menor = QR más chico
                    qr_width = qr_height = 126.5  # ← Cambia este número para TAMAÑO (126.5 = 10% más grande que 115)
                    # Ejemplos:
                    # 115 = tamaño original
                    # 126.5 = 10% más grande
                    # 138 = 20% más grande
                    # 103.5 = 10% más chico
                    
                    rect = fitz.Rect(qr_x, qr_y, qr_x + qr_width, qr_y + qr_height)
                    pg_permiso.insert_image(rect, pixmap=qr_pix, overlay=True)
            except Exception as e:
                print(f"[PDF] Error agregando QR: {e}")
            # ========================================
            # FIN AJUSTES DEL QR
            # ========================================
            
        else:
            # Plantilla básica si no existe
            doc_permiso = fitz.open()
            pg_permiso = doc_permiso.new_page(width=595, height=842)
            pg_permiso.insert_text((50, 50), "PERMISO AGUASCALIENTES (Plantilla no encontrada)", fontsize=20)
        
        # ===== PÁGINA 2: RECIBO =====
        if os.path.exists(PLANTILLA_RECIBO):
            doc_recibo = fitz.open(PLANTILLA_RECIBO)
            pg_recibo = doc_recibo[0]
            
            # ========================================
            # 📍 COORDENADAS DEL RECIBO - NO TOCAR
            # ========================================
            coords_recibo = {
                "recibo_ingreso_1": (469, 62, 10, (0, 0, 0)),  # Primera aparición
                "recibo_ingreso_2": (462, 771, 8, (0, 0, 0)),  # Segunda aparición
                "serie_folio": (469, 70, 7, (0, 0, 0)),       # Últimos 4 de serie + folio
                "pase_caja": (469, 83, 8, (0, 0, 0)),         # Pase a caja
                "fecha_hora": (469, 92, 7, (0, 0, 0)),        # Fecha y hora
                "rfc": (70, 165, 8, (0, 0, 0)),               # RFC
                "nombre": (70, 178, 8, (0, 0, 0)),            # Nombre
                "numero_1": (149, 291, 5, (0, 0, 0)),          # Primer número
                "numero_2": (190, 291, 5, (0, 0, 0)),          # Segundo número
            }
            # ========================================
            
            # Insertar recibo de ingreso (NEGRITA solo el primero)
            x1, y1, s1, col1 = coords_recibo["recibo_ingreso_1"]
            pg_recibo.insert_text((x1, y1), str(recibo_ingreso), fontsize=s1, color=col1, fontname="hebo")  # BOLD
            
            x2, y2, s2, col2 = coords_recibo["recibo_ingreso_2"]
            pg_recibo.insert_text((x2, y2), str(recibo_ingreso), fontsize=s2, color=col2)
            
            # Serie y folio
            serie_folio_texto = f"{ultimos_4_serie}  {datos['folio']}"
            x, y, s, col = coords_recibo["serie_folio"]
            pg_recibo.insert_text((x, y), serie_folio_texto, fontsize=s, color=col)
            
            # Pase a caja
            x, y, s, col = coords_recibo["pase_caja"]
            pg_recibo.insert_text((x, y), str(pase_caja), fontsize=s, color=col)
            
            # Fecha y hora
            x, y, s, col = coords_recibo["fecha_hora"]
            pg_recibo.insert_text((x, y), fecha_hora_completa, fontsize=s, color=col)
            
            # RFC
            x, y, s, col = coords_recibo["rfc"]
            pg_recibo.insert_text((x, y), rfc_generico, fontsize=s, color=col)
            
            # Nombre
            x, y, s, col = coords_recibo["nombre"]
            pg_recibo.insert_text((x, y), datos["nombre"], fontsize=s, color=col)
            
            # Números finales
            x1, y1, s1, col1 = coords_recibo["numero_1"]
            pg_recibo.insert_text((x1, y1), str(numero_1), fontsize=s1, color=col1)
            
            x2, y2, s2, col2 = coords_recibo["numero_2"]
            pg_recibo.insert_text((x2, y2), str(numero_2), fontsize=s2, color=col2)
        else:
            # Plantilla básica si no existe
            doc_recibo = fitz.open()
            pg_recibo = doc_recibo.new_page(width=595, height=842)
            pg_recibo.insert_text((50, 50), "RECIBO DE PAGO (Plantilla no encontrada)", fontsize=20)
        
        # ===== UNIFICAR PDFS =====
        doc_final = fitz.open()
        doc_final.insert_pdf(doc_permiso)  # Página 1: Permiso
        doc_final.insert_pdf(doc_recibo)   # Página 2: Recibo
        doc_final.save(out)
        
        # Cerrar documentos
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

        # BOTONES INLINE
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

# ------------ CALLBACK HANDLERS (BOTONES) ------------
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
    """Comando SERO + folio para validar manualmente"""
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

# ===================== FASTAPI =====================
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

app = FastAPI(lifespan=lifespan, title="Bot Permisos AGS", version="5.0.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_keep_task = None

# ===================== ENDPOINTS =====================

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
async def estado_folio(folio: str):
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
        
        hoy = datetime.now(ZoneInfo(TZ)).date()
        fecha_ven = datetime.fromisoformat(row['fecha_vencimiento']).date()
        vigente = hoy <= fecha_ven
        
        print(f"[CONSULTA] Folio encontrado: {folio_limpio}, Vigente: {vigente}")
        
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
                    <li><strong>Versión:</strong> 5.0 - PDF Unificado + Recibo</li>
                    <li><strong>Costo:</strong> ${PRECIO_PERMISO} MXN</li>
                    <li><strong>Tiempo límite:</strong> 36 horas</li>
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
    try:
        test_query = supabase.table("folios_registrados").select("count", count="exact").limit(1).execute()
        db_status = "conectado" if test_query else "error"
        
        bot_info = await bot.get_me()
        bot_status = f"@{bot_info.username}" if bot_info else "error"
        
        return {
            "status": "healthy",
            "timestamp": datetime.now(ZoneInfo(TZ)).isoformat(),
            "version": "5.0 - PDF Unificado + Recibo",
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
    print(f"[SISTEMA] Iniciando Bot Permisos Aguascalientes v5.0...")
    print(f"[SISTEMA] Base URL: {BASE_URL}")
    print(f"[SISTEMA] PDF UNIFICADO: Permiso + Recibo")
    print(f"[SISTEMA] Consecutivos: ACTIVOS")
    uvicorn.run(app, host="0.0.0.0", port=8000)
