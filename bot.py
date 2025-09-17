import os,json
import textwrap
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes, CallbackQueryHandler
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import openai
from dotenv import load_dotenv
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import re

# ===== Load env variables =====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
creds_dict = json.loads(os.getenv("GOOGLE_CREDS_JSON"))
# ===== Google Sheets setup =====
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("Trip Planner").sheet1 

# ===== Conversation states =====
NAME, DATES, NOT_FEASIBLE, DAYS, PEOPLE, BUDGET, REGION, KIDS, TYPE, CHOICES = range(10)

# ===== Start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Hi! Let's plan the family trip 🎉\n\nWhat's your name?")
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["Name"] = update.message.text
    await update.message.reply_text("Which dates are you available? (e.g., Dec 20–25)")
    return DATES

async def get_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["Dates Available"] = update.message.text
    await update.message.reply_text("Which dates are NOT feasible for you?")
    return NOT_FEASIBLE

async def get_not_feasible(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["Dates Not Feasible"] = update.message.text
    await update.message.reply_text("How many days can you travel?")
    return DAYS

async def get_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["No. of Days"] = update.message.text
    await update.message.reply_text("How many people from your side?")
    return PEOPLE

async def get_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["No. of People"] = update.message.text
    await update.message.reply_text("What is your budget per person? (e.g., 15k or 15000)")
    return BUDGET

async def get_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text.strip().lower()
    if user_input.endswith("k"):
        budget_value = int(float(user_input[:-1]) * 1000)
    else:
        try:
            budget_value = int(user_input)
        except ValueError:
            await update.message.reply_text("Please enter a valid budget (e.g., 15000 or 15k).")
            return BUDGET
    context.user_data["Budget Per Person"] = budget_value

    # Region selection inline
    keyboard = [
        [InlineKeyboardButton("Kerala", callback_data="Kerala"),
         InlineKeyboardButton("Tamil Nadu", callback_data="Tamil Nadu")],
        [InlineKeyboardButton("Karnataka", callback_data="Karnataka"),
         InlineKeyboardButton("Any", callback_data="Any")]
    ]
    await update.message.reply_text(
        "Which region do you prefer?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return REGION

# ===== Region, Kid-Friendly, Trip Type Buttons =====
async def button_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["Region Preference"] = query.data
    await query.edit_message_text(f"Selected Region: {query.data}")

    keyboard = [[InlineKeyboardButton("Yes", callback_data="Yes"),
                 InlineKeyboardButton("No", callback_data="No")]]
    await query.message.reply_text(
        "Do you need it to be kid-friendly?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return KIDS

async def button_kids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["Kid Friendly"] = query.data
    await query.edit_message_text(f"Kid-Friendly: {query.data}")

    keyboard = [[InlineKeyboardButton("Hills", callback_data="Hills"),
                 InlineKeyboardButton("Beach", callback_data="Beach"),
                 InlineKeyboardButton("Other", callback_data="Other")]]
    await query.message.reply_text(
        "Do you prefer Hills, Beach, or Other?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TYPE

async def button_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["Type Preference"] = query.data
    await query.edit_message_text(f"Type Preference: {query.data}")

    # Generate AI suggestions
    prompt = textwrap.dedent(f"""
    You are a realistic travel planner AI.
    User's preferences:
    - Trip Length: {context.user_data['No. of Days']} days
    - Number of People: {context.user_data['No. of People']}
    - Budget Per Person: ₹{context.user_data['Budget Per Person']}
    - Available Dates: {context.user_data['Dates Available']}
    - Preferred Region: {context.user_data['Region Preference']}
    - Kid Friendly: {context.user_data['Kid Friendly']}
    - Type Preference: {context.user_data['Type Preference']}

    Suggest 5 realistic destinations within 800 km from Chennai with total costs fitting budget, including transport(By self drive own car), accommodation, meals, and local travel.
    keep the dates and distance strictly. Need accurate distance for the place you suggest from chennai. 
    Output a numbered list only.
    """)
    response = openai_client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": prompt}]
)
    suggestions = response.choices[0].message.content
    context.user_data["Suggestions"] = suggestions

    await query.message.reply_text(
        f"Here are some suggestions:\n{suggestions}\n\n"
        "Reply with numbers of places you like (e.g., 1,3,5) or type your own separated by commas."
    )
    return CHOICES

# ===== Get user selected choices =====
async def get_choices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_reply = update.message.text
    selected = []
    suggestions = context.user_data.get("Suggestions", "").split("\n")

    for part in user_reply.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(suggestions):
                selected.append(suggestions[idx].strip("1234567890. "))
        else:
            selected.append(part)
    context.user_data["Selected Destinations"] = ", ".join(selected)

    # Save to Google Sheets
    row = [
        context.user_data.get("Name"),
        context.user_data.get("Dates Available"),
        context.user_data.get("Dates Not Feasible"),
        context.user_data.get("No. of Days"),
        context.user_data.get("No. of People"),
        context.user_data.get("Budget Per Person"),
        context.user_data.get("Region Preference"),
        context.user_data.get("Kid Friendly"),
        context.user_data.get("Type Preference"),
        context.user_data.get("Selected Destinations"),
    ]
    sheet.append_row(row)

    await update.message.reply_text(
        f"✅ Got it! Your destinations have been saved: {context.user_data['Selected Destinations']}\n"
        "You can now use /final to get the optimized group itinerary PDF."
    )
    return ConversationHandler.END

# ===== Cancel =====
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Cancelled. Start again with /start.")
    return ConversationHandler.END

# ===== Hi command =====
async def hi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available commands:\n"
        "/start - Start a new trip planning\n"
        "/summary - View all responses\n"
        "/final - Generate final optimized itinerary PDF\n"
        "/cancel - Cancel the current trip planning"
    )

# ===== Summary =====
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = sheet.get_all_records()
    if not data:
        await update.message.reply_text("No responses yet.")
        return
    itinerary_text = "Trip Summary:\n\n"
    for d in data:
        itinerary_text += f"{d['Name']}: {d['Selected Destinations']}\n"
    await update.message.reply_text(itinerary_text)




def expand_date_range(date_text):
    """
    Converts a string like 'Dec 20–22' to ['Dec 20', 'Dec 21', 'Dec 22'].
    Supports multiple ranges separated by commas.
    """
    dates = []
    parts = [p.strip() for p in date_text.split(",")]
    for part in parts:
        match = re.match(r'(\w+)\s*(\d+)[–-](\d+)', part)
        if match:
            month, start_day, end_day = match.groups()
            start_day = int(start_day)
            end_day = int(end_day)
            for day in range(start_day, end_day + 1):
                dates.append(f"{month} {day}")
        else:
            dates.append(part)
    return dates

def get_best_destination_and_dates():
    records = sheet.get_all_records()
    if not records:
        return None, None

    # Count destinations
    dest_counts = {}
    available_sets = []
    not_feasible_sets = []

    for r in records:
        # Clean destinations
        destinations = [d.strip() for d in r['Selected Destinations'].split(",") if d.strip()]
        for d in destinations:
            dest_counts[d] = dest_counts.get(d, 0) + 1

        # Expand available and not-feasible dates
        available_sets.append(set(expand_date_range(r.get('Dates Available', ''))))
        not_feasible_sets.append(set(expand_date_range(r.get('Dates Not Feasible', ''))))

    # Best destination: most selected
    best_dest = max(dest_counts, key=dest_counts.get) if dest_counts else None

    # Compute final valid dates
    if not available_sets:
        best_dates = None
    else:
        common_dates = set.intersection(*available_sets)  # dates everyone is available
        if not_feasible_sets:
            all_not_feasible = set.union(*not_feasible_sets)
            common_dates = common_dates - all_not_feasible  # remove not-feasible dates
        best_dates = sorted(common_dates) if common_dates else None

    return best_dest, best_dates


# ===== Generate Group PDF Itinerary =====
def generate_group_pdf_itinerary(filename="final_itinerary.pdf"):
    best_dest, best_dates = get_best_destination_and_dates()
    if not best_dest or not best_dates:
        return "Not enough data to generate itinerary."

    records = sheet.get_all_records()
    total_people = sum(int(r['No. of People']) for r in records)
    avg_days = int(sum(int(r['No. of Days']) for r in records)/len(records))
    avg_budget = int(sum(int(r['Budget Per Person']) for r in records)/len(records))

    prompt = textwrap.dedent(f"""
    You are a travel planner AI. Generate a final trip itinerary for a group:
    - Destination: {best_dest}
    - Dates: {', '.join(best_dates)}
    - Total People: {total_people}
    - Average Trip Length: {avg_days} days
    - Average Budget per Person: ₹{avg_budget}
    - Include kid-friendly activities if requested by any participant
    Provide a detailed day-wise itinerary in tabular format:
    | Day | Place/Activity | Meals | Transport | Accommodation | Estimated Cost (₹) |
    Also include a brief summary at the end.
    """)
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    itinerary_text = response.choices[0].message.content

    # Create PDF
    doc = SimpleDocTemplate(filename, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    elements.append(Paragraph(f"📌 Final Trip Itinerary for {best_dest}", styles['Title']))
    elements.append(Spacer(1, 12))

    lines = [line.strip() for line in itinerary_text.split("\n") if line.strip()]
    data = []
    for line in lines:
        if "|" in line:
            row = [cell.strip() for cell in line.split("|")[1:-1]]
            data.append(row)
    if data:
        table = Table([["Day","Place/Activity","Meals","Transport","Accommodation","Estimated Cost (₹)"]] + data, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#4CAF50')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.whitesmoke),
            ('ALIGN',(0,0),(-1,-1),'CENTER'),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('GRID',(0,0),(-1,-1),1,colors.black),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.whitesmoke, colors.lightgrey])
        ]))
        elements.append(table)
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(f"Dates: {', '.join(best_dates)} | Destination: {best_dest} | Total People: {total_people} | Avg Budget/Person: ₹{avg_budget}", styles['Normal']))
    doc.build(elements)
    return filename

# ===== /final command =====
async def final_itinerary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filename = generate_group_pdf_itinerary()
    if filename.endswith(".pdf"):
        await update.message.reply_document(document=open(filename, "rb"))
    else:
        await update.message.reply_text(filename)

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
            REGION: [CallbackQueryHandler(button_region)],
            KIDS: [CallbackQueryHandler(button_kids)],
            TYPE: [CallbackQueryHandler(button_type)],
            CHOICES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_choices)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^(hi|hello)$", re.I)), hi))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("final", final_itinerary))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
