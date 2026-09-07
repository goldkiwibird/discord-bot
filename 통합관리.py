import functools
import asyncio
from datetime import timedelta
import json
import os
import re
import aiohttp
from aiohttp import web
from bs4 import BeautifulSoup
import discord
from discord.ext import commands, tasks

# ---------------------------------------------------------
# 기본 설정 및 변수
# ---------------------------------------------------------
CONFIG_FILE = "통합관리.json"
DATA_FILE = "system_data.json"

PORTAL_URL = 'https://hybeim.oqupie.com/portal/2797/notice'
API_LIST_URL = 'https://hybeim.oqupie.com/api/knowledge/2816/notification?subdomain=hybeim&all=1&page_number=1&limit=20'
API_DETAIL_BASE_URL = 'https://hybeim.oqupie.com/api/knowledge/2816/notification/'

# Gemini API 키 (Render 환경변수 GEMINI_API_KEY 읽기)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://hybeim.oqupie.com/portal/2797/notice',
    'Origin': 'https://hybeim.oqupie.com',
    'portal-id': '2797'
}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, max_messages=10000)

config = {}
user_voice_channels = {}   
voice_channel_owners = {}  
voice_empty_tasks = {}     
user_text_threads = {}     
text_thread_owners = {}    
web_server_started = False
bot_initialized = False
blocked_attachment_message_ids = set()

# 백업 워커가 1건당 1.5초 이상 걸리므로, 100건이면 최악의 경우도 몇 분 내에 소화됩니다.
# 평소 업로드 버스트는 이 안에서 조용히 흡수하고, 진짜 비정상적으로 몰릴 때만 대기가 걸리게 하기 위한 값입니다.
BACKUP_QUEUE_MAXSIZE = 100
backup_queue = asyncio.Queue(maxsize=BACKUP_QUEUE_MAXSIZE)
gemini_session = None  # 통신 세션 유지를 위한 전역 변수 추가

# ---------------------------------------------------------
# Render 전용 HTTP 간이 웹서버
# ---------------------------------------------------------
async def start_web_server():
    global web_server_started
    if web_server_started:
        return
    web_server_started = True

    app = web.Application()
    app.router.add_get('/', lambda req: web.Response(text="Discord Bot is online!"))
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Render 간이 웹서버 구동 완료 (포트: {port})")

# ---------------------------------------------------------
# 데이터 로드 및 저장
# ---------------------------------------------------------
def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"❌ 설정 파일 로드 실패: {e}")

def save_data():
    tmp_path = f"{DATA_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({
                "user_voice_channels": user_voice_channels,
                "voice_channel_owners": voice_channel_owners,
                "user_text_threads": user_text_threads,
                "text_thread_owners": text_thread_owners
            }, f)
            f.flush()
            os.fsync(f.fileno())
        # 임시 파일을 완성한 뒤 원자적으로 교체 -> 저장 도중 강제 종료돼도 기존 파일은 온전함
        os.replace(tmp_path, DATA_FILE)
    except OSError as e:
        print(f"⚠️ 저장 실패: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass

def load_data():
    global user_voice_channels, voice_channel_owners, user_text_threads, text_thread_owners
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                user_voice_channels = {int(k): int(v) for k, v in data.get("user_voice_channels", {}).items()}
                voice_channel_owners = {int(k): int(v) for k, v in data.get("voice_channel_owners", {}).items()}
                user_text_threads = {int(k): int(v) for k, v in data.get("user_text_threads", {}).items()}
                text_thread_owners = {int(k): int(v) for k, v in data.get("text_thread_owners", {}).items()}
        except Exception as e:
            print(f"❌ 세션 데이터 로드 실패: {e}")

def get_msg(system_key: str, lang: str, msg_key: str, default: str = ""):
    return config.get(system_key, {}).get("messages", {}).get(lang, {}).get(msg_key, default)

def get_channel_name(key: str, default: str) -> str:
    return config.get("backup_config", {}).get(key, default)

def get_attachment_lang(channel) -> str:
    """메시지가 작성된 카테고리를 기준으로 안내 문구 언어를 결정합니다."""
    category = getattr(channel, "category", None)

    # 스레드인 경우 부모 채널의 카테고리 확인
    if category is None:
        parent = getattr(channel, "parent", None)
        category = getattr(parent, "category", None)

    if category:
        category_name = category.name

        # rules_config의 언어별 카테고리 설정 확인
        rule_categories = config.get("rules_config", {}).get("categories", {})
        for lang, configured_category_name in rule_categories.items():
            if category_name == configured_category_name:
                return lang

        # text_config의 카테고리 설정 확인
        text_categories = config.get("text_config", {}).get("categories", {})
        if category_name in text_categories:
            return text_categories[category_name].get("lang", "en")

    return config.get("attachment_config", {}).get("default_lang", "en")


# 파일 종류와 관계없이 파일당 25MiB(26,214,400바이트)까지 허용합니다.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# ---------------------------------------------------------
# 1. 규칙 동의 시스템
# ---------------------------------------------------------
class RuleAgreementView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn_config = config.get("rules_config", {}).get("buttons", {}).get(lang, {})
        
        agree_btn = discord.ui.Button(label=btn_config.get("agree", "Agree"), style=discord.ButtonStyle.success, custom_id=f"rule_agree_{lang}")
        agree_btn.callback = self.agree_callback
        self.add_item(agree_btn)
        
        disagree_btn = discord.ui.Button(label=btn_config.get("disagree", "Disagree"), style=discord.ButtonStyle.danger, custom_id=f"rule_disagree_{lang}")
        disagree_btn.callback = self.disagree_callback
        self.add_item(disagree_btn)

    async def agree_callback(self, interaction: discord.Interaction):
        role_name = config.get("rules_config", {}).get("roles", {}).get(self.lang)
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        msg = config.get("rules_config", {}).get("messages", {}).get(self.lang, {})
        if role:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(msg.get("agree", "Done"), ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 역할 '{role_name}'을 찾을 수 없습니다.", ephemeral=True)

    async def disagree_callback(self, interaction: discord.Interaction):
        role_name = config.get("rules_config", {}).get("roles", {}).get(self.lang)
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        msg = config.get("rules_config", {}).get("messages", {}).get(self.lang, {})
        if role and role in interaction.user.roles:
            await interaction.user.remove_roles(role)
        await interaction.response.send_message(msg.get("disagree", "Canceled"), ephemeral=True)

# ---------------------------------------------------------
# 2. 음성 채널 생성 시스템
# ---------------------------------------------------------
async def delete_voice_channel_timer(channel_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if channel:
            lang = "en"
            if channel.category:
                cat_conf = config.get("voice_config", {}).get("categories", {}).get(channel.category.name)
                if cat_conf:
                    lang = cat_conf["lang"]
            reason_msg = get_msg("voice_config", lang, "delete_reason_empty", "자동 삭제")
            await channel.delete(reason=reason_msg)

        owner_id = voice_channel_owners.get(channel_id)
        user_voice_channels.pop(owner_id, None)
        voice_channel_owners.pop(channel_id, None)
        save_data()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[음성 채널 삭제 오류] Channel ID {channel_id}: {e}")
    finally:
        voice_empty_tasks.pop(channel_id, None)

class VoiceCapacityModal(discord.ui.Modal):
    def __init__(self, default_name: str, lang: str):
        title = get_msg("voice_config", lang, "modal_title", "Create Voice Channel")
        super().__init__(title=title)
        self.lang = lang
        self.name_input = discord.ui.TextInput(label=get_msg("voice_config", lang, "modal_name_label", "Channel Name"), default=default_name, min_length=1, max_length=45, required=True)
        self.add_item(self.name_input)
        self.limit_input = discord.ui.TextInput(label=get_msg("voice_config", lang, "modal_limit_label", "Max Capacity (2~99)"), default="4", min_length=1, max_length=2, required=True)
        self.add_item(self.limit_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        try:
            user_limit = int(self.limit_input.value)
            if not (2 <= user_limit <= 99):
                return await interaction.response.send_message(get_msg("voice_config", self.lang, "capacity_range_error"), ephemeral=True)
        except ValueError:
            return await interaction.response.send_message(get_msg("voice_config", self.lang, "capacity_type_error"), ephemeral=True)

        category = interaction.channel.category
        overwrites = dict(category.overwrites) if category else {}
        user_overwrite = overwrites.get(interaction.user, discord.PermissionOverwrite())
        user_overwrite.manage_channels = True
        overwrites[interaction.user] = user_overwrite

        try:
            new_channel = await interaction.guild.create_voice_channel(name=self.name_input.value, category=category, user_limit=user_limit, overwrites=overwrites)
        except discord.HTTPException as e:
            msg = get_msg("voice_config", self.lang, "category_full_error") if e.code == 50024 else "❌ Error creating channel."
            return await interaction.response.send_message(msg, ephemeral=True)

        user_voice_channels[user_id] = new_channel.id
        voice_channel_owners[new_channel.id] = user_id
        save_data()

        if interaction.user.voice:
            try: await interaction.user.move_to(new_channel)
            except discord.HTTPException: pass

        if len(new_channel.members) == 0:
            voice_empty_tasks[new_channel.id] = asyncio.create_task(delete_voice_channel_timer(new_channel.id, 300))

        succ_msg = get_msg("voice_config", self.lang, "creation_success").format(mention=new_channel.mention)
        await interaction.response.send_message(succ_msg, ephemeral=True)

class VoiceChannelCreateView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn = discord.ui.Button(label=get_msg("voice_config", lang, "create_button", "Create Voice Channel"), style=discord.ButtonStyle.green, custom_id=f"create_vc_button_{lang}")
        btn.callback = self.create_callback
        self.add_item(btn)

    async def create_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id in user_voice_channels:
            channel_id = user_voice_channels[user_id]
            try: existing_channel = await interaction.guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.HTTPException): existing_channel = None

            if existing_channel:
                msg = get_msg("voice_config", self.lang, "already_have_channel").format(mention=existing_channel.mention)
                return await interaction.response.send_message(msg, ephemeral=True)
            else:
                user_voice_channels.pop(user_id, None)
                voice_channel_owners.pop(channel_id, None)
                save_data()

        default_fmt = get_msg("voice_config", self.lang, "modal_name_default", "{name}'s Room")
        await interaction.response.send_modal(VoiceCapacityModal(default_fmt.format(name=interaction.user.display_name), self.lang))

# ---------------------------------------------------------
# 3. 비공개 텍스트 스레드 시스템
# ---------------------------------------------------------
class InviteActionView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        self.select = discord.ui.UserSelect(placeholder="\u2800", min_values=1, max_values=25, row=0)
        self.select.callback = self.select_callback
        self.add_item(self.select)
        
        self.confirm_btn = discord.ui.Button(label=get_msg("text_config", lang, "confirm_button", "Confirm"), style=discord.ButtonStyle.primary, row=1)
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

    async def confirm_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not hasattr(self.select, 'values') or not self.select.values:
            return await interaction.followup.send(get_msg("text_config", self.lang, "select_user_error"), ephemeral=True)

        thread = interaction.channel
        added_users, admin_users = [], []

        for member in self.select.values:
            if member.guild_permissions.administrator:
                admin_users.append(member.display_name)
            else:
                await thread.add_user(member)
                added_users.append(member.mention)

        result_msg = ""
        if added_users: result_msg += get_msg("text_config", self.lang, "invite_success").format(users=', '.join(added_users))
        if admin_users: result_msg += get_msg("text_config", self.lang, "invite_admin_skip").format(admins=', '.join(admin_users))
        if not result_msg: result_msg = get_msg("text_config", self.lang, "invite_empty_error")

        await interaction.followup.send(result_msg, ephemeral=True)

class InviteButtonView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn = discord.ui.Button(label=get_msg("text_config", lang, "invite_button", "Invite"), style=discord.ButtonStyle.green, custom_id=f"invite_user_button_{lang}", emoji="✉️")
        btn.callback = self.invite_callback
        self.add_item(btn)

    async def invite_callback(self, interaction: discord.Interaction):
        if interaction.user.id != text_thread_owners.get(interaction.channel.id):
            return await interaction.response.send_message(get_msg("text_config", self.lang, "invite_only_owner_error"), ephemeral=True)
        await interaction.response.send_message(get_msg("text_config", self.lang, "invite_menu_desc"), view=InviteActionView(self.lang), ephemeral=True)

class TextThreadModal(discord.ui.Modal):
    def __init__(self, default_name: str, lang: str):
        super().__init__(title=get_msg("text_config", lang, "modal_title", "Create Private Thread"))
        self.lang = lang
        self.name_input = discord.ui.TextInput(label=get_msg("text_config", lang, "modal_name_label", "Thread Name"), default=default_name, min_length=1, max_length=45, required=True)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_thread = await interaction.channel.create_thread(name=self.name_input.value, type=discord.ChannelType.private_thread, invitable=False)
            await new_thread.add_user(interaction.user)
            
            user_text_threads[interaction.user.id] = new_thread.id
            text_thread_owners[new_thread.id] = interaction.user.id
            save_data()

            await interaction.response.send_message(get_msg("text_config", self.lang, "creation_success"), ephemeral=True)
            await new_thread.send(get_msg("text_config", self.lang, "channel_guide_text").format(mention=interaction.user.mention), view=InviteButtonView(self.lang))
        except Exception as e:
            await interaction.response.send_message(f"❌ 오류가 발생했습니다: {e}", ephemeral=True)

class TextChannelCreateView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn = discord.ui.Button(label=get_msg("text_config", lang, "create_button", "Create Thread"), style=discord.ButtonStyle.blurple, custom_id=f"create_text_channel_button_{lang}")
        btn.callback = self.create_callback
        self.add_item(btn)

    async def create_callback(self, interaction: discord.Interaction):
        default_fmt = get_msg("text_config", self.lang, "modal_name_default", "{name}'s Thread")
        await interaction.response.send_modal(TextThreadModal(default_fmt.format(name=interaction.user.display_name), self.lang))

@tasks.loop(hours=1)
async def check_inactive_threads():
    now = discord.utils.utcnow()
    has_changed = False
    for thread_id, owner_id in list(text_thread_owners.items()):
        try:
            thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            last_time = thread.created_at if thread.last_message_id is None else discord.utils.snowflake_time(thread.last_message_id)
            if now - last_time > timedelta(days=30):
                await thread.delete()
                text_thread_owners.pop(thread_id, None)
                user_text_threads.pop(owner_id, None)
                has_changed = True
        except discord.NotFound:
            text_thread_owners.pop(thread_id, None)
            user_text_threads.pop(owner_id, None)
            has_changed = True
        except Exception: pass
            
    if has_changed: save_data()

# ---------------------------------------------------------
# 4. 공지사항 웹후크 크롤러
# ---------------------------------------------------------
def html_to_text(html_content: str) -> str:
    text = BeautifulSoup(html_content, "html.parser").get_text(separator="\n").strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = text.split('\n')
    if len(lines) > 15: return '\n'.join(lines[:15]) + '\n\n... (Omitted below)'
    elif len(text) > 1500: return text[:1500] + '\n\n... (Omitted below)'
    return text

async def process_notices():
    print("\n🔍 공지사항 스캔 시작...")

    webhook_url = config.get("notice_config", {}).get("webhook_url")
    if not webhook_url:
        print("❌ notice_config.webhook_url이 설정되어 있지 않습니다.")
        return

    recent_urls, target_channel = [], None
    categories = config.get("rules_config", {}).get("categories", {})
    
    for guild in bot.guilds:
        cat_name = categories.get("en")
        category = discord.utils.get(guild.categories, name=cat_name)
        if category:
            target_channel = discord.utils.get(category.text_channels, name="📢공지사항")
            if target_channel: break

    if target_channel:
        try:
            async for msg in target_channel.history(limit=20):
                if msg.embeds:
                    for embed in msg.embeds:
                        if embed.url: recent_urls.append(embed.url)
                if msg.content:
                    recent_urls.extend([word.strip() for word in msg.content.split() if word.startswith("https://hybeim.oqupie.com/portal/2797/notice/")])
        except Exception: pass

    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            await session.get(PORTAL_URL)
            await asyncio.sleep(1)
            async with session.get(API_LIST_URL) as resp:
                if resp.status != 200: return
                list_data = await resp.json()

            notices = list_data.get("result", {}).get("list", [])
            if not notices and isinstance(list_data.get("result"), list): notices = list_data.get("result")

            for notice in reversed(notices[:20]):
                notice_id = notice.get("id") or notice.get("notification_id") or notice.get("article_id")
                if not notice_id: continue
                web_link = f"https://hybeim.oqupie.com/portal/2797/notice/{notice_id}"
                if web_link in recent_urls: continue

                async with session.get(f"{API_DETAIL_BASE_URL}{notice_id}?subdomain=hybeim") as d_resp:
                    if d_resp.status != 200: continue
                    clean_text = html_to_text((await d_resp.json()).get("result", {}).get("content", "본문 없음"))

                content = f"📢 **{notice.get('title')}**\n\n**Preview:**\n```\n{clean_text}\n```\n**View Details:**\n{web_link}"
                async with session.post(webhook_url, json={"content": content}) as wh_resp:
                    if wh_resp.status in (200, 204): print(f"✅ 새 공지 전송 성공: {notice.get('title')}")
    except Exception as e: print(f"❌ 공지 크롤링 수집 오류: {e}")

@bot.command(name="공지")
@commands.has_permissions(administrator=True)
async def fetch_notices_command(ctx):
    try: await ctx.message.delete()
    except Exception: pass
    await process_notices()

# ---------------------------------------------------------
# 5. 비동기 백업 큐 워커 & 이벤트
# ---------------------------------------------------------
async def enqueue_backup(task: dict):
    """백업 큐에 작업을 넣는다. 큐가 가득 찬 경우 자리가 날 때까지 대기하되,
    비정상적으로 몰리고 있다는 걸 알 수 있도록 경고 로그를 한 번 남긴다."""
    if backup_queue.full():
        print(f"⚠️ 백업 큐가 가득 찼습니다 (최대 {BACKUP_QUEUE_MAXSIZE}건). 처리될 때까지 대기합니다...")
    await backup_queue.put(task)

async def backup_worker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            task = await backup_queue.get()
            message = task['message']
            task_type = task['type']
            
            if task_type == 'image':
                await process_image_backup(message)
            elif task_type == 'forum':
                await process_forum_backup(message)
                
            backup_queue.task_done()
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"⚠️ 백업 워커 에러: {e}")

async def process_image_backup(message):
    target = get_channel_name("attachment_log_channel", "이미지-백업-로그")
    img_log_channel = discord.utils.get(message.guild.text_channels, name=target)
    if not img_log_channel: return
        
    saved_files = []
    for att in message.attachments:
        if att.size <= MAX_ATTACHMENT_BYTES:
            try: saved_files.append(await att.to_file())
            except Exception: pass
    
    if saved_files:
        content = message.content if message.content else "(텍스트 없음)"
        
        # 🛑 4096자 초과 방지
        if len(content) > 3800:
            content = content[:3800] + "\n\n... (원문이 너무 길어 생략됨)"
            
        embed = discord.Embed(
            description=f"**[➡️ 원본 메시지로 이동하기]({message.jump_url})**\n\n```\n{content}\n```", 
            color=discord.Color.blue()
        )
        embed.add_field(name="📍 위치", value=message.channel.mention, inline=True)
        embed.add_field(name="✍️ 작성자", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
        
        try: await img_log_channel.send(embed=embed, files=saved_files)
        except Exception: pass

async def process_forum_backup(message):
    target = get_channel_name("forum_log_channel", "포럼-백업-로그")
    backup_channel = discord.utils.get(message.guild.text_channels, name=target)
    if not backup_channel: return
        
    content = message.content if message.content else "(본문 텍스트 없음)"
    saved_files, attachment_names = [], []
    if message.attachments:
        for att in message.attachments:
            if att.size <= MAX_ATTACHMENT_BYTES:
                try:
                    saved_files.append(await att.to_file())
                    attachment_names.append(att.filename)
                except Exception: attachment_names.append(f"{att.filename} (백업 실패)")
            else: attachment_names.append(f"{att.filename} (용량 초과)")
        content += f"\n\n📎 **첨부파일:** {', '.join(attachment_names)}"

    # 🛑 4096자 초과 방지
    if len(content) > 3800:
        content = content[:3800] + "\n\n... (원문 및 첨부파일 목록이 너무 길어 생략됨)"

    embed = discord.Embed(
        description=f"**게시글 제목:** `{message.channel.name}`\n**[➡️ 원본 게시글로 이동하기]({message.jump_url})**\n\n```\n{content}\n```", 
        color=discord.Color.green()
    )
    embed.add_field(name="📍 위치", value=f"📰 {message.channel.parent.mention}", inline=True)
    embed.add_field(name="✍️ 작성자", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
    
    try: await backup_channel.send(embed=embed, files=saved_files)
    except Exception: pass

async def translate_with_gemini(text: str, target_lang: str) -> str:
    if not GEMINI_API_KEY or not gemini_session:
        print("⚠️ GEMINI_API_KEY 또는 gemini_session이 설정되어 있지 않습니다.")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    # 1. JSON 파일에서 전체 용어집 로드
    glossary_dict = config.get("glossary", {}).get(target_lang, {})
    
    # 2. 메시지(text)에 실제 포함된 단어만 파이썬이 사전에 필터링 (대소문자 무시 비교)
    matched_glossary = {
        k: v for k, v in glossary_dict.items() 
        if k.lower() in text.lower()
    }
    
    # 3. 매칭된 단어가 있을 때만 AI 지시문에 규칙 추가
    glossary_rule = ""
    if matched_glossary:
        rules = ", ".join([f"'{k}' as '{v}'" for k, v in matched_glossary.items()])
        glossary_rule = f"**[Glossary Rule: Always translate {rules} when the target language is '{target_lang}'.]** "

    prompt = (
        f"You are a translator for a gaming Discord community. "
        f"Translate the following chat message naturally into the target language code '{target_lang}' (e.g. 'ko' -> Korean). "
        f"Preserve original tone, emojis, and game slang where appropriate. "
        f"{glossary_rule}"
        f"Output ONLY the translated text with no extra commentary, quotes, or markdown code blocks:\n\n{text}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    # 전체 통신 25초, 연결 5초의 커스텀 타임아웃
    custom_timeout = aiohttp.ClientTimeout(total=25, connect=5)

    for attempt in range(3):
        try:
            # 매번 새로 연결하지 않고 봇이 켜질 때 생성된 전역 세션을 재사용
            async with gemini_session.post(url, headers=headers, json=payload, timeout=custom_timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        return candidates[0]["content"]["parts"][0]["text"].strip()
                elif resp.status == 503:
                    print(f"⚠️ 구글 서버 순간 과부하(503). 재시도 중... ({attempt + 1}/3)")
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                else:
                    err_body = await resp.text()
                    print(f"⚠️ Gemini API 오류: HTTP {resp.status} - {err_body}")
                    return None
        except asyncio.TimeoutError:
            print(f"⏰ Gemini API 응답 시간 초과 (재시도 {attempt + 1}/3)")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"⚠️ Gemini API 요청 실패 ({type(e).__name__}): {e}")
            await asyncio.sleep(1)

    return None

@bot.event
async def on_message(message):
    # 봇이 작성한 메시지이거나 DM인 경우 무시
    if message.author.bot or not message.guild:
        return

    # 포럼의 첫 번째 게시글인지 판별
    is_forum_starter = (
        isinstance(message.channel, discord.Thread)
        and isinstance(message.channel.parent, discord.ForumChannel)
        and message.id == message.channel.id
    )

    # ---------------------------------------------------------
    # 파일당 25MB를 초과하는 첨부파일이 있으면 메시지 전체 차단
    # ---------------------------------------------------------
    if message.attachments:
        has_oversized_attachment = any(
            attachment.size > MAX_ATTACHMENT_BYTES
            for attachment in message.attachments
        )

        if has_oversized_attachment:
            lang = get_attachment_lang(message.channel)

            warning_message = get_msg(
                "attachment_config",
                lang,
                "file_size_error",
                "❌ {mention} Your entire message was deleted because at least one attachment exceeds 25MB. Each file must be 25MB or smaller."
            ).format(mention=message.author.mention)

            # on_message_delete에서 해당 파일을 다시 백업하지 않도록 기록
            blocked_attachment_message_ids.add(message.id)

            try:
                await message.delete()
            except discord.Forbidden:
                blocked_attachment_message_ids.discard(message.id)
                print(
                    f"⚠️ 파일 차단 실패: 메시지 삭제 권한이 없습니다. "
                    f"Channel ID: {message.channel.id}"
                )
                return
            except discord.HTTPException as e:
                blocked_attachment_message_ids.discard(message.id)
                print(f"⚠️ 파일 차단 중 메시지 삭제 실패: {e}")
                return

            # 혹시 삭제 이벤트가 발생하지 않는 경우를 대비해
            # 60초 후 차단 기록 자동 정리
            asyncio.get_running_loop().call_later(
                60,
                blocked_attachment_message_ids.discard,
                message.id
            )

            try:
                await message.channel.send(
                    warning_message,
                    delete_after=10
                )
            except discord.HTTPException as e:
                print(f"⚠️ 파일 차단 안내 메시지 전송 실패: {e}")

            # 삭제된 메시지는 번역/백업/명령어 처리하지 않음
            return

    # ---------------------------------------------------------
    # 카테고리 기반 자동 번역 모니터링 (Gemini 모델 적용)
    # ---------------------------------------------------------
    if message.content and message.channel.category:
        cat_name = message.channel.category.name
        trans_config = config.get("translation_config", {}).get("categories", {})

        if cat_name in trans_config:
            target_channel_name = trans_config[cat_name]["target_channel"]
            target_lang = trans_config[cat_name]["target_lang"]

            trans_channel = discord.utils.get(
                message.guild.text_channels,
                name=target_channel_name
            )

            if trans_channel:
                try:
                    text_to_translate = message.content[:3000]

                    if is_forum_starter:
                        # 포럼 첫 글인 경우 제목과 본문을 동시에 비동기 번역
                        translated_title, translated_text = await asyncio.gather(
                            translate_with_gemini(message.channel.name, target_lang),
                            translate_with_gemini(text_to_translate, target_lang)
                        )

                        display_title = (
                            translated_title
                            if translated_title
                            else message.channel.name
                        )
                    else:
                        # 일반 채팅이나 스레드 댓글인 경우 본문만 번역
                        translated_text = await translate_with_gemini(
                            text_to_translate,
                            target_lang
                        )
                        display_title = message.channel.name

                    if translated_text:
                        embed = discord.Embed(
                            description=f"```\n{translated_text}\n```",
                            color=discord.Color.teal()
                        )

                        embed.set_author(
                            name=f"{message.author.display_name}  •  #{display_title}",
                            url=message.jump_url,
                            icon_url=(
                                message.author.display_avatar.url
                                if message.author.display_avatar
                                else None
                            )
                        )

                        await trans_channel.send(embed=embed)

                except Exception as e:
                    print(f"⚠️ 자동 번역 모니터링 오류: {e}")

    # ---------------------------------------------------------
    # 이미지 백업
    # ---------------------------------------------------------
    if message.attachments and not is_forum_starter:
        target = get_channel_name(
            "attachment_log_channel",
            "이미지-백업-로그"
        )

        img_log_channel = discord.utils.get(
            message.guild.text_channels,
            name=target
        )

        if img_log_channel and message.channel.id != img_log_channel.id:
            await enqueue_backup({
                'type': 'image',
                'message': message
            })

    # ---------------------------------------------------------
    # 포럼 백업
    # ---------------------------------------------------------
    if is_forum_starter:
        await enqueue_backup({
            'type': 'forum',
            'message': message
        })

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message):
    # 첨부파일 용량 제한으로 삭제한 메시지는
    # 삭제 로그 및 첨부파일 백업 대상에서 제외
    if message.id in blocked_attachment_message_ids:
        blocked_attachment_message_ids.discard(message.id)
        return

    if message.author.bot or not message.guild:
        return

    target = get_channel_name(
        "delete_log_channel",
        "메시지-삭제-로그"
    )

    log_channel = discord.utils.get(
        message.guild.text_channels,
        name=target
    )

    if not log_channel:
        return

    channel_display = message.channel.mention

    if isinstance(message.channel, discord.Thread):
        parent = message.channel.parent

        if parent:
            if isinstance(parent, discord.ForumChannel):
                channel_display = (
                    f"📰 {parent.mention} (포럼) > "
                    f"📄 {message.channel.mention} (게시글)"
                )
            else:
                channel_display = (
                    f"💬 {parent.mention} > "
                    f"🧵 {message.channel.mention} (스레드)"
                )

    deleter = message.author

    try:
        # 감사 로그 반영에 지연이 있을 수 있어 짧게 재시도하고,
        # 비슷한 시간대에 다른 삭제가 섞여 있어도 놓치지 않도록 최근 항목 여러 개를 확인한다.
        for attempt in range(3):
            await asyncio.sleep(0.5 if attempt == 0 else 1)

            found_match = False
            async for entry in message.guild.audit_logs(
                limit=5,
                action=discord.AuditLogAction.message_delete
            ):
                now = discord.utils.utcnow()
                if (now - entry.created_at).total_seconds() >= 5:
                    continue

                extra = getattr(entry, "extra", None)
                extra_channel = getattr(extra, "channel", None)

                extra_channel_id = (
                    getattr(extra_channel, "id", None)
                    if extra_channel
                    else None
                )

                if (
                    entry.target
                    and entry.target.id == message.author.id
                    and extra_channel_id == message.channel.id
                ):
                    deleter = entry.user
                    found_match = True
                    break

            if found_match:
                break

    except Exception:
        pass

    content = (
        message.content
        if message.content
        else "(본문 텍스트 없음)"
    )

    saved_files = []
    attachment_names = []

    if message.attachments:
        for att in message.attachments:
            if att.size <= MAX_ATTACHMENT_BYTES:
                try:
                    saved_files.append(await att.to_file())
                    attachment_names.append(att.filename)
                except Exception:
                    attachment_names.append(
                        f"{att.filename} (다운로드 실패)"
                    )
            else:
                attachment_names.append(
                    f"{att.filename} (용량 초과)"
                )

        content += (
            f"\n\n📎 **첨부파일:** "
            f"{', '.join(attachment_names)}"
        )

    # 4096자 초과 방지
    if len(content) > 3800:
        content = (
            content[:3800]
            + "\n\n... (삭제된 원문이 너무 길어 생략됨)"
        )

    embed = discord.Embed(
        description=f"```\n{content}\n```",
        color=discord.Color.red()
    )

    embed.add_field(
        name="📍 삭제된 위치",
        value=channel_display,
        inline=False
    )

    embed.add_field(
        name="✍️ 작성자",
        value=f"{message.author.mention} (`{message.author.id}`)",
        inline=True
    )

    embed.add_field(
        name="🛠️ 삭제한 유저",
        value=f"{deleter.mention} (`{deleter.id}`)",
        inline=True
    )

    embed.add_field(
        name="🕒 작성 시간",
        value=f"<t:{int(message.created_at.timestamp())}:F>",
        inline=False
    )

    embed.set_footer(
        text=f"Message ID: {message.id}"
    )

    try:
        await log_channel.send(
            embed=embed,
            files=saved_files
        )
    except Exception:
        pass

@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    if not entry.guild:
        return

    # JSON의 backup_config에서 'manage_log_channel' (기본값: '관리-로그') 가져오기
    manage_channel_name = get_channel_name("manage_log_channel", "관리-로그")
    manage_log_channel = discord.utils.get(entry.guild.text_channels, name=manage_channel_name)

    if not manage_log_channel:
        return

    moderator = entry.user
    target = entry.target
    reason = entry.reason or "작성된 사유 없음"
    should_send = False

    embed = discord.Embed(timestamp=discord.utils.utcnow())
    embed.set_author(name=f"처리한 관리자: {moderator.display_name}", icon_url=moderator.display_avatar.url if moderator.display_avatar else None)
    
    target_display = f"{target} (`{target.id}`)" if hasattr(target, 'id') else str(target)
    embed.add_field(name="대상 유저", value=target_display, inline=False)
    
    # 1. 추방 (Kick)
    if entry.action == discord.AuditLogAction.kick:
        embed.title = "🔨 유저 추방 (Kick)"
        embed.color = discord.Color.orange()
        embed.add_field(name="작성한 제재 사유", value=reason, inline=False)
        should_send = True

    # 2. 차단 (Ban)
    elif entry.action == discord.AuditLogAction.ban:
        embed.title = "⛔ 유저 차단 (Ban)"
        embed.color = discord.Color.red()
        embed.add_field(name="작성한 제재 사유", value=reason, inline=False)
        should_send = True

    # 3. 차단 해제 (Unban)
    elif entry.action == discord.AuditLogAction.unban:
        embed.title = "🔓 유저 차단 해제 (Unban)"
        embed.color = discord.Color.green()
        embed.add_field(name="작성한 제재 사유", value=reason, inline=False)
        should_send = True

    # 4. 타임아웃 및 닉네임 강제 변경
    elif entry.action == discord.AuditLogAction.member_update:
        if hasattr(entry.after, 'timed_out_until'):
            timeout_before = getattr(entry.before, 'timed_out_until', None)
            timeout_after = entry.after.timed_out_until
            
            if timeout_after is not None:
                embed.title = "⏳ 타임아웃 적용"
                embed.color = discord.Color.gold()
                embed.add_field(name="설정된 타임아웃 기간 (해제 시점)", value=f"<t:{int(timeout_after.timestamp())}:F>", inline=False)
                embed.add_field(name="작성한 제재 사유", value=reason, inline=False)
                should_send = True
            elif timeout_before is not None and timeout_after is None:
                embed.title = "⌛ 타임아웃 조기 해제"
                embed.color = discord.Color.dark_gold()
                embed.add_field(name="작성한 제재 사유", value=reason, inline=False)
                should_send = True

        if hasattr(entry.after, 'nick'):
            old_nick = getattr(entry.before, 'nick', '없음') or '없음'
            new_nick = entry.after.nick or '없음'
            if moderator.id != target.id:
                embed.title = "📝 서버 닉네임 강제 변경"
                embed.color = discord.Color.blue()
                embed.add_field(name="변경 전 닉네임", value=old_nick, inline=True)
                embed.add_field(name="변경 후 닉네임", value=new_nick, inline=True)
                embed.add_field(name="작성한 변경 사유", value=reason, inline=False)
                should_send = True

    # 5. 역할 제거 (봇이 제거한 경우는 제외)
    elif entry.action == discord.AuditLogAction.member_role_update:
        if moderator.bot:
            return

        removed_roles = getattr(entry.before, 'roles', [])
        if removed_roles:
            embed.title = "➖ 역할 강제 제거"
            embed.color = discord.Color.dark_theme()
            role_names = ", ".join([role.name for role in removed_roles])
            embed.add_field(name="제거된 역할", value=role_names, inline=False)
            embed.add_field(name="작성한 사유", value=reason, inline=False)
            should_send = True

    # 통합 '관리-로그' 채널로 전송
    if should_send:
        try:
            await manage_log_channel.send(embed=embed)
        except Exception as e:
            print(f"⚠️ 관리-로그 전송 실패: {e}")

async def setup_rules_channel(guild):
    r_cats = config.get("rules_config", {}).get("categories", {})
    r_chans = config.get("rules_config", {}).get("channels", {})
    for lang in ["en", "zh-TW"]:
        cat = discord.utils.get(guild.categories, name=r_cats.get(lang))
        if cat:
            chan = discord.utils.get(cat.text_channels, name=r_chans.get(lang))
            if chan:
                # 1. 기존 봇 메시지를 찾아서 삭제
                async for m in chan.history(limit=20):
                    if m.author == bot.user:
                        try:
                            await m.delete()
                        except Exception:
                            pass
                
                # 2. 새로운 규칙 메시지 전송
                rule_text = config.get("rules_config", {}).get("rules", {}).get(lang, "")
                embed = discord.Embed(description=rule_text, color=discord.Color.blue())
                await chan.send(embed=embed, view=RuleAgreementView(lang))
                
async def setup_voice_channel(guild):
    v_cats = config.get("voice_config", {}).get("categories", {})
    for cat_name, conf in v_cats.items():
        category = discord.utils.get(guild.categories, name=cat_name)
        if category:
            chan = discord.utils.get(category.text_channels, name=conf["channel"])
            if chan:
                async for m in chan.history(limit=20):
                    if m.author == bot.user: await m.delete()
                await chan.send(get_msg("voice_config", conf["lang"], "notice_text"), view=VoiceChannelCreateView(conf["lang"]))

async def setup_text_channel(guild):
    t_cats = config.get("text_config", {}).get("categories", {})
    for cat_name, conf in t_cats.items():
        category = discord.utils.get(guild.categories, name=cat_name)
        if category:
            chan = discord.utils.get(category.text_channels, name=conf["channel"])
            if chan:
                async for m in chan.history(limit=20):
                    if m.author == bot.user: await m.delete()
                await chan.send(get_msg("text_config", conf["lang"], "notice_text"), view=TextChannelCreateView(conf["lang"]))

@bot.event
async def on_ready():
    print(f"✅ 통합 관리 봇 로그인 완료: {bot.user}")

    # 봇이 켜질 때 세션을 전역으로 한 번만 생성하여 유지 (속도 대폭 향상)
    global gemini_session
    if gemini_session is None:
        gemini_session = aiohttp.ClientSession()

    await start_web_server()
    load_config()
    load_data()

    # on_ready는 재연결(세션 재-IDENTIFY)될 때마다 다시 호출될 수 있으므로,
    # 한 번만 실행돼야 하는 초기화는 bot_initialized로 가드
    global bot_initialized
    if not bot_initialized:
        bot.loop.create_task(backup_worker())

        for lang in ["en", "zh-TW"]:
            bot.add_view(RuleAgreementView(lang))
            bot.add_view(VoiceChannelCreateView(lang))
            bot.add_view(TextChannelCreateView(lang))
            bot.add_view(InviteButtonView(lang))

        if not check_inactive_threads.is_running(): check_inactive_threads.start()

    has_cleaned_voice = False
    for channel_id, owner_id in list(voice_channel_owners.items()):
        try:
            channel = await bot.fetch_channel(channel_id)
            if len(channel.members) == 0:
                voice_empty_tasks[channel_id] = asyncio.create_task(delete_voice_channel_timer(channel_id, 300))
        except (discord.NotFound, discord.HTTPException):
            voice_channel_owners.pop(channel_id, None)
            user_voice_channels.pop(owner_id, None)
            has_cleaned_voice = True

    if has_cleaned_voice: save_data()

    has_cleaned_text = False
    for thread_id, owner_id in list(text_thread_owners.items()):
        try: await bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.HTTPException):
            text_thread_owners.pop(thread_id, None)
            user_text_threads.pop(owner_id, None)
            has_cleaned_text = True

    if has_cleaned_text: save_data()

    if not bot_initialized:
        for guild in bot.guilds:
            try:
                await setup_rules_channel(guild)
                await setup_voice_channel(guild)
                await setup_text_channel(guild)
            except Exception as e: print(f"⚠️ [{guild.name}] 초기화 중 오류: {e}")

        bot_initialized = True

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel and len(before.channel.members) == 0:
        c_id = before.channel.id
        if c_id in voice_channel_owners:
            voice_empty_tasks[c_id] = asyncio.create_task(delete_voice_channel_timer(c_id, 300))

    if after.channel and after.channel.id in voice_empty_tasks:
        voice_empty_tasks[after.channel.id].cancel()
        voice_empty_tasks.pop(after.channel.id, None)

TOKEN = os.getenv("BOT_TOKEN", "")
bot.run(TOKEN)
