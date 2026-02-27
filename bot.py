import os
import json
import base64
import re
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler
import anthropic
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SHEET_NAME = os.environ.get("SHEET_NAME", "Наташа готовые экспорт")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    return sheet

def ensure_headers(sheet):
    headers = ["Дата", "Бренд", "Model Name", "Version Name", "Цвет", "Model Year", "Year", "Цена завода", "FOB Хоргос +5%"]
    first_row = sheet.row_values(1)
    if first_row != headers:
        sheet.insert_row(headers, 1)

def extract_year(text):
    """Извлекает год из строки, например '2026 60km Free' -> 2026"""
    match = re.search(r'20\d{2}', str(text))
    if match:
        return match.group(0)
    return ""

def write_to_sheet(sheet, rows):
    today = datetime.now().strftime("%d.%m.%Y")
    data = []
    for row in rows:
        version = row.get("version", "")
        model_year = row.get("model_year", "") or extract_year(version)
        year = row.get("year", "") or model_year

        # FOB + 5% округлённый до сотен
        try:
            fob_num = float(str(row.get("price_fob", "0")).replace(",", "").replace(" ", "").replace("$", ""))
            fob_plus = round(fob_num * 1.05 / 100) * 100
        except:
            fob_plus = ""

        data.append([
            today,
            row.get("brand", ""),
            row.get("model", ""),
            version,
            row.get("color", ""),
            model_year,
            year,
            row.get("price_cny", ""),
            fob_plus,
        ])
    sheet.append_rows(data, value_input_option="USER_ENTERED")
    return len(data)

def parse_price_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> list:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """Ты парсишь прайс-лист автомобилей. Извлеки ВСЕ строки из таблицы.

Для каждого автомобиля верни JSON объект с полями:
- brand: бренд автомобиля (BYD, Geely, Changan, Toyota и т.д.)
- model: название модели (Yuan UP, Starship 7, Han EV и т.д.)
- version: комплектация / версия (полное описание)
- color: цвет автомобиля (если указан, иначе пустая строка)
- model_year: модельный год (ищи в названии комплектации, например "2026 60km Free" -> "2026". Если не найден — пустая строка)
- year: год выпуска (обычно совпадает с model_year, если есть отдельная колонка Year — бери оттуда)
- price_cny: цена завода (колонка "indicative price" — только число без символов валюты)
- price_fob: цена FOB Хоргос (колонка "FOB horgos USD" — только число без символов валюты и знака $)

ВАЖНО: Верни ТОЛЬКО JSON массив. Никакого текста до или после.
Пример:
[{"brand":"BYD","model":"Yuan UP","version":"Intelligent Driving 401KM transcendence","color":"White gray","model_year":"2025","year":"2025","price_cny":"119800","price_fob":"14700"}]

Если прайс на китайском — транслитерируй или переведи названия на английский."""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ]
    )

    text = response.content[0].text.strip()
    logger.info(f"Claude response (first 500 chars): {text[:500]}")

    if "```" in text:
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            text = match.group(1).strip()

    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start:end+1]

    rows = json.loads(text)
    return rows

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📥 Получил картинку, обрабатываю прайс...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        await msg.edit_text("🔍 Распознаю данные с помощью AI...")
        rows = parse_price_image(bytes(image_bytes), "image/jpeg")
        if not rows:
            await msg.edit_text("❌ Не удалось найти данные. Попробуй другую картинку.")
            return
        await msg.edit_text(f"📊 Найдено {len(rows)} позиций, записываю в таблицу...")
        sheet = get_sheet()
        ensure_headers(sheet)
        count = write_to_sheet(sheet, rows)
        await msg.edit_text(f"✅ Готово! Добавлено {count} позиций в таблицу.\n📋 Вкладка: {SHEET_NAME}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        await msg.edit_text("❌ Ошибка при разборе данных. Попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        msg = await update.message.reply_text("📥 Получил файл, обрабатываю...")
        try:
            file = await context.bot.get_file(doc.file_id)
            image_bytes = await file.download_as_bytearray()
            await msg.edit_text("🔍 Распознаю данные...")
            rows = parse_price_image(bytes(image_bytes), doc.mime_type)
            if not rows:
                await msg.edit_text("❌ Данные не найдены.")
                return
            sheet = get_sheet()
            ensure_headers(sheet)
            count = write_to_sheet(sheet, rows)
            await msg.edit_text(f"✅ Готово! Добавлено {count} позиций в таблицу.")
        except Exception as e:
            logger.error(f"Error: {e}")
            await msg.edit_text(f"❌ Ошибка: {str(e)}")
    else:
        await update.message.reply_text("Пришли картинку прайса (фото или изображение).")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для обработки прайсов авто.\n\n"
        "📸 Пришли мне картинку прайса — я распознаю все позиции и запишу в Google таблицу.\n\n"
        "Поддерживаю прайсы на английском и китайском языке."
    )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
