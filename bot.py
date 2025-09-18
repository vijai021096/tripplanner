# full_bot_ready_for_railway.py
import os
import json
import textwrap
import re
from collections import Counter

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes, CallbackQueryHandler
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import openai
from dotenv import load_dotenv
from fpdf import FPDF

# ===== Load env variables =====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")  # optional
DEJAVU_FONT_PATH = os.getenv("DEJAVU_FONT_PATH", "DejaVuSans.ttf")  # ensure this file exists in project root
DEJAVU_FONT_BOLD_PATH = os.getenv("DEJAVU_FONT_BOLD_PATH", "DejaVuSans-Bold.ttf")
DEJAVU_FONT_ITALIC_PATH = os.getenv("DEJAVU_FONT_ITALIC_PATH", "DejaVuSans-Oblique.ttf")
DEJAVU_FONT_BOLDITALIC_PATH = os.getenv("DEJAVU_FONT_BOLDITALIC_PATH", "DejaVuSans-BoldOblique.ttf")


# validate critical envs early
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing. Set it in .env or Railway variables.")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing. Set it in .env or Railway variables.")

# OpenAI client (keeps your usage consistent with earlier code)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ===== Google Sheets setup (supports env JSON or local file) =====
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
if GOOGLE_CREDS_JSON:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
else:
    # fallback to local filename (ensure file is present on the server or in repo - but don't commit secrets)
    json_path = "trip-planner-472402-76b33256a47b.json"
    if not os.path.exists(json_path):
        raise RuntimeError("Google credentials JSON not found. Provide GOOGLE_CREDS_JSON env or upload the JSON file.")
    creds = ServiceAccountCredentials.from_json_keyfile_name(json_path, scope)

client = gspread.authorize(creds)
sheet = client.open("Trip Planner").sheet1

# ===== Conversation states =====
NAME, DATES, NOT_FEASIBLE, DAYS, PEOPLE, BUDGET, REGION, KIDS, TYPE, CHOICES = range(10)

# ===== Utility functions =====
def safe_strip_number_prefix(text: str) -> str:
    """Remove leading numbering like '1. Place - detail' or '1) Place'."""
    return re.sub(r'^\s*\d+\s*[\.\)-]*\s*', '', text).strip()

def expand_date_range(date_text):
    """
    Convert inputs like:
      "Dec 20–22, Dec 25" -> ["Dec 20","Dec 21","Dec 22","Dec 25"]
      "Dec 20, Dec 21" -> ["Dec 20","Dec 21"]
    Non-matching parts are returned as-is (stripped).
    """
    if not date_text:
        return []
    
    # Ensure it is string
    date_text = str(date_text)
    
    dates = []
    parts = [p.strip() for p in re.split(r',|\n', date_text) if p.strip()]
    for part in parts:
        # match 'Dec 20-22' or 'Dec 20–22' or 'Dec 20 - 22'
        m = re.match(r'([A-Za-z]+)\s*(\d+)\s*[–-]\s*(\d+)', part)
        if m:
            month, start, end = m.groups()
            start_i, end_i = int(start), int(end)
            for d in range(start_i, end_i + 1):
                dates.append(f"{month} {d}")
        else:
            dates.append(part)
    return dates


def intersect_available_minus_notfeasible(records):
    """
    Compute dates where all users are available AND not present in any 'not feasible' lists.
    Returns sorted list or empty list.
    """
    available_sets = []
    not_feasible_sets = []

    for r in records:
        # Convert to string safely
        av_str = str(r.get('Dates Available', '') or '').strip()
        nf_str = str(r.get('Dates Not Feasible', '') or '').strip()

        av_set = set(expand_date_range(av_str)) if av_str else set()
        nf_set = set(expand_date_range(nf_str)) if nf_str else set()

        available_sets.append(av_set)
        not_feasible_sets.append(nf_set)

    if not available_sets:
        return []

    # Intersection of all available sets (strict: every user must have date)
    common = set.intersection(*available_sets) if available_sets else set()
    if not common:
        return []

    # Remove any union of not feasible dates
    union_nf = set.union(*not_feasible_sets) if not_feasible_sets else set()
    final = sorted(common - union_nf)
    return final


# ===== Handlers: user flow =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Hi! Let's plan the family trip 🎉\n\nWhat's your name?")
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["Name"] = update.message.text.strip()
    await update.message.reply_text("Which dates are you available? (e.g., Dec 20–22 or Dec 20, Dec 21)")
    return DATES

async def get_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["Dates Available"] = update.message.text.strip()
    await update.message.reply_text("Which dates are NOT feasible for you? (e.g., Dec 24 or Dec 24–25). If none, type 'none'")
    return NOT_FEASIBLE

async def get_not_feasible(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nf = update.message.text.strip()
    context.user_data["Dates Not Feasible"] = '' if nf.lower() in ('none','no','n/a','na','') else nf
    await update.message.reply_text("How many days can you travel?")
    return DAYS

async def get_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["No. of Days"] = update.message.text.strip()
    await update.message.reply_text("How many people from your side?")
    return PEOPLE

async def get_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["No. of People"] = update.message.text.strip()
    await update.message.reply_text("What is your budget per person? (e.g., 15k or 15000)")
    return BUDGET

async def get_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text.strip().lower()
    if user_input.endswith("k"):
        try:
            budget_value = int(float(user_input[:-1]) * 1000)
        except:
            await update.message.reply_text("Please enter a valid budget (e.g., 15000 or 15k).")
            return BUDGET
    else:
        try:
            budget_value = int(user_input)
        except ValueError:
            await update.message.reply_text("Please enter a valid budget (e.g., 15000 or 15k).")
            return BUDGET
    context.user_data["Budget Per Person"] = budget_value

    keyboard = [
        [InlineKeyboardButton("Kerala", callback_data="Kerala"),
         InlineKeyboardButton("Tamil Nadu", callback_data="Tamil Nadu")],
        [InlineKeyboardButton("Karnataka", callback_data="Karnataka"),
         InlineKeyboardButton("Any", callback_data="Any")]
    ]
    await update.message.reply_text("Which region do you prefer?", reply_markup=InlineKeyboardMarkup(keyboard))
    return REGION

async def button_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["Region Preference"] = query.data
    await query.edit_message_text(f"Selected Region: {query.data}")

    keyboard = [[InlineKeyboardButton("Yes", callback_data="Yes"),
                 InlineKeyboardButton("No", callback_data="No")]]
    await query.message.reply_text("Do you need it to be kid-friendly?", reply_markup=InlineKeyboardMarkup(keyboard))
    return KIDS

async def button_kids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["Kid Friendly"] = query.data
    await query.edit_message_text(f"Kid-Friendly: {query.data}")

    keyboard = [[InlineKeyboardButton("Hills", callback_data="Hills"),
                 InlineKeyboardButton("Beach", callback_data="Beach"),
                 InlineKeyboardButton("Other", callback_data="Other")]]
    await query.message.reply_text("Do you prefer Hills, Beach, or Other?", reply_markup=InlineKeyboardMarkup(keyboard))
    return TYPE

async def button_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["Type Preference"] = query.data
    await query.edit_message_text(f"Type Preference: {query.data}")

    # Prepare prompt and call OpenAI
    prompt = textwrap.dedent(f"""
    You are a realistic travel planner AI.

    User preferences:
    - Trip Length: {context.user_data.get('No. of Days')}
    - Number of People: {context.user_data.get('No. of People')}
    - Budget Per Person: ₹{context.user_data.get('Budget Per Person')}
    - Available Dates: {context.user_data.get('Dates Available')}
    - Preferred Region: {context.user_data.get('Region Preference')}
    - Kid Friendly: {context.user_data.get('Kid Friendly')}
    - Type Preference: {context.user_data.get('Type Preference')}

    Rules:
    - Suggest 5 realistic destinations **within 800 km from Chennai**.
    - For each suggestion include the exact distance (in km) from Chennai (do not hallucinate — if unsure, skip).
    - Include estimated total costs per person (transport for self-drive, stay, meals, local travel).
    - Only include destinations whose **total cost per person does not exceed the budget**.
    - Output a numbered list (1-5). Each line should be: "1. Place — Distance: XXX km — Reason/Cost summary".
    - Output nothing else.
    """)
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        suggestions = resp.choices[0].message.content.strip()
    except Exception as e:
        suggestions = "Sorry, couldn't generate suggestions. Try again later."

    context.user_data["Suggestions"] = suggestions
    await query.message.reply_text(
        f"Here are some suggestions:\n{suggestions}\n\nReply with numbers of places you like (e.g., 1,3) or type your own destinations separated by commas."
    )
    return CHOICES

async def get_choices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_reply = update.message.text
    selected = []
    suggestions_text = context.user_data.get("Suggestions", "")
    # create list of suggestion names by extracting after number and before '—' or '-' or '('
    suggestion_lines = [ln.strip() for ln in suggestions_text.splitlines() if ln.strip()]
    suggestion_names = []
    for ln in suggestion_lines:
        # remove numbering then take up to '—' or '-' or '('
        s = re.sub(r'^\s*\d+\s*[\.\)-]*\s*', '', ln)
        # split by em dash or en dash or hyphen or parentheses
        s = re.split(r'—|-|\(|;', s)[0].strip()
        suggestion_names.append(s)

    for part in user_reply.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(suggestion_names):
                selected.append(suggestion_names[idx])
        else:
            # custom place provided by user
            selected.append(part)
    context.user_data["Selected Destinations"] = ", ".join([s.strip() for s in selected if s.strip()])

    # Save to Google Sheets (strings)
    row = [
        context.user_data.get("Name", ""),
        context.user_data.get("Dates Available", ""),
        context.user_data.get("Dates Not Feasible", ""),
        context.user_data.get("No. of Days", ""),
        context.user_data.get("No. of People", ""),
        str(context.user_data.get("Budget Per Person", "")),
        context.user_data.get("Region Preference", ""),
        context.user_data.get("Kid Friendly", ""),
        context.user_data.get("Type Preference", ""),
        context.user_data.get("Selected Destinations", "")
    ]
    try:
        sheet.append_row(row)
    except Exception as e:
        await update.message.reply_text(f"Saved locally but failed to append to Google Sheets: {e}")
        # still proceed

    await update.message.reply_text(
        f"✅ Got it! Your destinations have been saved: {context.user_data.get('Selected Destinations')}\n"
        "You can now use /final to get the optimized group itinerary PDF."
    )
    return ConversationHandler.END

# ===== PDF generation (Unicode) =====
def parse_itinerary_table_from_ai(text: str):
    """Return list of rows (each row is list of 6 cells) parsed from a markdown-style table in AI response."""
    rows = []
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()]:
        if "|" in line:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) == 6:
                rows.append(cells)
    return rows

# ===== PDF generation (Unicode) =====
def parse_itinerary_table_from_ai(text: str):
    """Return list of rows (each row is list of 6 cells) parsed from a markdown-style table in AI response."""
    rows = []
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()]:
        if "|" in line:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) == 6:
                rows.append(cells)
    return rows

def generate_group_pdf_itinerary(filename="final_itinerary.pdf"):
    records = sheet.get_all_records()
    if not records:
        return "No responses yet."

    best_dest, best_dates = None, []
    # compute best destination (most selected)
    dest_counts = Counter()
    for r in records:
        for d in [x.strip() for x in (r.get('Selected Destinations') or "").split(",") if x.strip()]:
            dest_counts[d] += 1
    if dest_counts:
        best_dest = dest_counts.most_common(1)[0][0]

    best_dates = intersect_available_minus_notfeasible(records)
    if not best_dest or not best_dates:
        return "Not enough data to generate itinerary (no common feasible dates or no selected destinations)."

    total_people = sum(int(r.get('No. of People') or 0) for r in records)
    avg_days = int(sum(int(r.get('No. of Days') or 0) for r in records) / len(records))
    avg_budget = int(sum(int(r.get('Budget Per Person') or 0) for r in records) / len(records))

    prompt = textwrap.dedent(f"""
You are a realistic travel planner AI.

Group trip info:
- Total trip length: {avg_days} days
- Total people: {total_people}
- Budget per person: ₹{avg_budget}
- Available dates: {', '.join(best_dates)}
- User-selected destinations (popularity considered): {', '.join([d for d, _ in dest_counts.most_common()])}
- Kid-friendly if requested

Instructions:
1. Each destination has an ideal stay:
   Varkala: 2 days
   Kodaikanal: 3 days
   Munnar: 3 days
   Ooty: 3 days
   Mahabalipuram: 1 day
   Coorg: 3 days
   Yelagiri: 1 day
2. Allocate days per destination according to ideal duration and total trip length and travel time also from chennai and return to chennai by car.
3. If the top destination’s ideal duration is shorter than the trip, fill remaining days with next most popular destinations that are feasible and close by, minimizing travel.
4. Provide a **day-wise itinerary in a table**:

| Day | Place/Activity | Meals | Transport | Accommodation | Estimated Cost (₹) |
|-----|----------------|-------|----------|---------------|--------------------|

5. Ensure realistic cost estimates and travel feasibility.
6. Be concise, do not exceed the total trip length.
7. Output nothing else.
"""
)
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        itinerary_text = resp.choices[0].message.content
    except Exception as e:
        return f"Error generating itinerary: {e}"

    # parse table rows
    data_rows = parse_itinerary_table_from_ai(itinerary_text)

    # Create PDF with Unicode font
    pdf = FPDF()
    pdf.add_page()
    # add font (ensure DEJAVU_FONT_PATH exists)
    if os.path.exists(DEJAVU_FONT_PATH):
        pdf.add_font("DejaVu", "", DEJAVU_FONT_PATH, uni=True)
        pdf.add_font("DejaVu", "B", DEJAVU_FONT_BOLD_PATH, uni=True)
        pdf.add_font("DejaVu", "I", DEJAVU_FONT_ITALIC_PATH, uni=True)  # Italic
        pdf.add_font("DejaVu", "BI", DEJAVU_FONT_BOLDITALIC_PATH, uni=True)  # Bold Italic
        title_font = ("DejaVu", "B", 16)
        header_font = ("DejaVu", "B", 12)
        body_font = ("DejaVu", "", 11)
        info_font = ("DejaVu", "I", 10)
    else:
        # fallback to built-in font (no Unicode) and replace ₹ with Rs
        title_font = ("Arial", "B", 16)
        header_font = ("Arial", "B", 12)
        body_font = ("Arial", "", 11)
        info_font = ("Arial", "I", 10)
        itinerary_text = itinerary_text.replace("₹", "Rs ")

    pdf.set_font(*title_font)
    pdf.cell(0, 10, f"Final Trip Itinerary — {best_dest}", ln=True, align="C")
    pdf.ln(6)

    # table header
    headers = ["Day", "Place/Activity", "Meals", "Transport", "Accommodation", "Estimated Cost (₹)"]
    col_widths = [15, 60, 30, 30, 40, 25]
    pdf.set_font(*header_font)
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], 9, h, 1, 0, "C")
    pdf.ln()

    # rows
    pdf.set_font(*body_font)
    if data_rows:
        for row in data_rows:
            for i, cell in enumerate(row):
                text = str(cell)
                # truncate long cell content to avoid layout issues
                pdf.cell(col_widths[i], 8, text[:40], 1)
            pdf.ln()
    else:
        pdf.cell(0, 8, "No detailed day-wise table parsed from AI output.", 1, ln=True)

    pdf.ln(6)
    # summary lines (non-table lines)
    summary_lines = [ln for ln in itinerary_text.splitlines() if '|' not in ln and ln.strip()]
    pdf.set_font(*body_font)
    for ln in summary_lines:
        pdf.multi_cell(0, 7, ln)

    pdf.ln(4)
    pdf.set_font(*info_font)
    pdf.multi_cell(0, 6, f"Dates: {', '.join(best_dates)} | Destination: {best_dest} | Total People: {total_people} | Avg Budget/Person: ₹{avg_budget}")

    # save file
    pdf.output(filename)
    return filename

# ===== Commands =====
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Cancelled. Start again with /start.")
    return ConversationHandler.END

async def hi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available commands:\n"
        "/start - Start a new trip planning\n"
        "/summary - View all responses\n"
        "/final - Generate final optimized itinerary PDF\n"
        "/cancel - Cancel the current trip planning"
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = sheet.get_all_records()
    if not data:
        await update.message.reply_text("No responses yet.")
        return
    s = "Trip Summary:\n\n"
    for d in data:
        s += f"{d.get('Name','')}: {d.get('Selected Destinations','')}\n"
    await update.message.reply_text(s)

async def final_itinerary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generating final itinerary PDF — please wait...")
    result = generate_group_pdf_itinerary()
    if result and result.endswith(".pdf") and os.path.exists(result):
        with open(result, "rb") as f:
            await update.message.reply_document(document=f)
        try:
            os.remove(result)
        except Exception:
            pass
    else:
        await update.message.reply_text(str(result))

# ===== Main =====
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            DATES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_dates)],
            NOT_FEASIBLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_not_feasible)],
            DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_days)],
            PEOPLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_people)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_budget)],
            REGION: [CallbackQueryHandler(button_region, pattern="^(Kerala|Tamil Nadu|Karnataka|Any)$")],
            KIDS: [CallbackQueryHandler(button_kids, pattern="^(Yes|No)$")],
            TYPE: [CallbackQueryHandler(button_type, pattern="^(Hills|Beach|Other)$")],
            CHOICES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_choices)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    # respond to /hi command and plain "hi"/"hello"
    app.add_handler(CommandHandler("hi", hi))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^\s*(hi|hello)\s*$", re.I)), hi))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("final", final_itinerary))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
