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

WEBHOOK_URL = 'https://discord.com/api/webhooks/1544543154789687346/B_1pjOuPdwKWYEEeqlSMZtQvUjxNXNQVQvdf1PQ_lJgFzDiRdP_bzyB_SCrEAyL9KjlQ'
PORTAL_URL = 'https://hybeim.oqupie.com/portal/2797/notice'
API_LIST_URL = 'https://hybeim.oqupie.com/api/knowledge/2816/notification?subdomain=hybeim&all=1&page_number=1&limit=20'
API_DETAIL_BASE_URL = 'https://hybeim.oqupie.com/api/knowledge/2816/notification/'

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

bot = commands.Bot(command_prefix="!", intents=intents)

# 상태 데이터
config = {}
user_voice_channels = {}   # {user_id: channel_id}
voice_channel_owners = {}  # {channel_id: user_id}
voice_empty_tasks = {}     # {channel_id: Task}

user_text_threads = {}     # {user_id: thread_id}
text_thread_owners = {}    # {thread_id: user_id}

web_server_started = False  # 웹서버 중복 구동 방지 플래그

# ---------------------------------------------------------
# Render 전용 HTTP 간이 웹서버 (24시간 슬립 방지용)
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
    
    # Render가 자동으로 부여하는 포트 번호 수신 (기본 10000)
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Render 간이 웹서버 구동 완료 (포트: {port})")

# ---------------------------------------------------------
# 데이터 로드 및 저장 (용량 부족 예외 처리 포함)
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
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "user_voice_channels": user_voice_channels,
                "voice_channel_owners": voice_channel_owners,
                "user_text_threads": user_text_threads,
                "text_thread_owners": text_thread_owners
            }, f)
    except OSError as e:
        print(f"⚠️ 저장 실패: {e}")

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

# ---------------------------------------------------------
# 1. 규칙 동의 및 역할 부여 시스템
# ---------------------------------------------------------
class RuleAgreementView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn_config = config.get("rules_config", {}).get("buttons", {}).get(lang, {})
        
        agree_btn = discord.ui.Button(
            label=btn_config.get("agree", "Agree"),
            style=discord.ButtonStyle.success,
            custom_id=f"rule_agree_{lang}"
        )
        agree_btn.callback = self.agree_callback
        self.add_item(agree_btn)
        
        disagree_btn = discord.ui.Button(
            label=btn_config.get("disagree", "Disagree"),
            style=discord.ButtonStyle.danger,
            custom_id=f"rule_disagree_{lang}"
        )
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

        self.name_input = discord.ui.TextInput(
            label=get_msg("voice_config", lang, "modal_name_label", "Channel Name"),
            default=default_name,
            min_length=1,
            max_length=45,
            required=True
        )
        self.add_item(self.name_input)

        self.limit_input = discord.ui.TextInput(
            label=get_msg("voice_config", lang, "modal_limit_label", "Max Capacity (2~99)"),
            default="4",
            min_length=1,
            max_length=2,
            required=True
        )
        self.add_item(self.limit_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        try:
            user_limit = int(self.limit_input.value)
            if not (2 <= user_limit <= 99):
                await interaction.response.send_message(get_msg("voice_config", self.lang, "capacity_range_error"), ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message(get_msg("voice_config", self.lang, "capacity_type_error"), ephemeral=True)
            return

        category = interaction.channel.category
        
        # 카테고리 기본 권한을 복사한 후, 생성자에게 채널 관리(삭제) 권한 추가
        overwrites = dict(category.overwrites) if category else {}
        user_overwrite = overwrites.get(interaction.user, discord.PermissionOverwrite())
        user_overwrite.manage_channels = True
        overwrites[interaction.user] = user_overwrite

        try:
            new_channel = await interaction.guild.create_voice_channel(
                name=self.name_input.value,
                category=category,
                user_limit=user_limit,
                overwrites=overwrites
            )
        except discord.HTTPException as e:
            if e.code == 50024:
                await interaction.response.send_message(get_msg("voice_config", self.lang, "category_full_error"), ephemeral=True)
            else:
                await interaction.response.send_message("❌ Error creating channel.", ephemeral=True)
            return

        user_voice_channels[user_id] = new_channel.id
        voice_channel_owners[new_channel.id] = user_id
        save_data()

        if interaction.user.voice:
            try:
                await interaction.user.move_to(new_channel)
            except discord.HTTPException:
                pass

        if len(new_channel.members) == 0:
            task = asyncio.create_task(delete_voice_channel_timer(new_channel.id, 300))
            voice_empty_tasks[new_channel.id] = task

        succ_msg = get_msg("voice_config", self.lang, "creation_success").format(mention=new_channel.mention)
        await interaction.response.send_message(succ_msg, ephemeral=True)

class VoiceChannelCreateView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn_label = get_msg("voice_config", lang, "create_button", "Create Voice Channel")
        btn = discord.ui.Button(
            label=btn_label,
            style=discord.ButtonStyle.green,
            custom_id=f"create_vc_button_{lang}"
        )
        btn.callback = self.create_callback
        self.add_item(btn)

    async def create_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id in user_voice_channels:
            channel_id = user_voice_channels[user_id]
            try:
                existing_channel = await interaction.guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.HTTPException):
                existing_channel = None

            if existing_channel:
                msg = get_msg("voice_config", self.lang, "already_have_channel").format(mention=existing_channel.mention)
                await interaction.response.send_message(msg, ephemeral=True)
                return
            else:
                user_voice_channels.pop(user_id, None)
                voice_channel_owners.pop(channel_id, None)
                save_data()

        default_fmt = get_msg("voice_config", self.lang, "modal_name_default", "{name}'s Room")
        default_name = default_fmt.format(name=interaction.user.display_name)
        await interaction.response.send_modal(VoiceCapacityModal(default_name, self.lang))

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
        
        confirm_label = get_msg("text_config", lang, "confirm_button", "Confirm")
        self.confirm_btn = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.primary, row=1)
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

    async def confirm_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not hasattr(self.select, 'values') or not self.select.values:
            await interaction.followup.send(get_msg("text_config", self.lang, "select_user_error"), ephemeral=True)
            return

        thread = interaction.channel
        added_users, admin_users = [], []

        for member in self.select.values:
            if member.guild_permissions.administrator:
                admin_users.append(member.display_name)
            else:
                await thread.add_user(member)
                added_users.append(member.mention)

        result_msg = ""
        if added_users:
            result_msg += get_msg("text_config", self.lang, "invite_success").format(users=', '.join(added_users))
        if admin_users:
            result_msg += get_msg("text_config", self.lang, "invite_admin_skip").format(admins=', '.join(admin_users))
        if not result_msg:
            result_msg = get_msg("text_config", self.lang, "invite_empty_error")

        await interaction.followup.send(result_msg, ephemeral=True)

class InviteButtonView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn_label = get_msg("text_config", lang, "invite_button", "Invite")
        btn = discord.ui.Button(
            label=btn_label,
            style=discord.ButtonStyle.green,
            custom_id=f"invite_user_button_{lang}",
            emoji="✉️"
        )
        btn.callback = self.invite_callback
        self.add_item(btn)

    async def invite_callback(self, interaction: discord.Interaction):
        owner_id = text_thread_owners.get(interaction.channel.id)
        if interaction.user.id != owner_id:
            await interaction.response.send_message(get_msg("text_config", self.lang, "invite_only_owner_error"), ephemeral=True)
            return
        
        await interaction.response.send_message(
            get_msg("text_config", self.lang, "invite_menu_desc"),
            view=InviteActionView(self.lang),
            ephemeral=True
        )

class TextThreadModal(discord.ui.Modal):
    def __init__(self, default_name: str, lang: str):
        super().__init__(title=get_msg("text_config", lang, "modal_title", "Create Private Thread"))
        self.lang = lang

        self.name_input = discord.ui.TextInput(
            label=get_msg("text_config", lang, "modal_name_label", "Thread Name"),
            default=default_name,
            min_length=1,
            max_length=45,
            required=True
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        try:
            new_thread = await interaction.channel.create_thread(
                name=self.name_input.value,
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            await new_thread.add_user(interaction.user)
            
            user_text_threads[user_id] = new_thread.id
            text_thread_owners[new_thread.id] = user_id
            save_data()

            await interaction.response.send_message(get_msg("text_config", self.lang, "creation_success"), ephemeral=True)
            guide = get_msg("text_config", self.lang, "channel_guide_text").format(mention=interaction.user.mention)
            await new_thread.send(guide, view=InviteButtonView(self.lang))
        except Exception as e:
            await interaction.response.send_message(f"❌ 오류가 발생했습니다: {e}", ephemeral=True)

class TextChannelCreateView(discord.ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        btn_label = get_msg("text_config", lang, "create_button", "Create Thread")
        btn = discord.ui.Button(
            label=btn_label,
            style=discord.ButtonStyle.blurple,
            custom_id=f"create_text_channel_button_{lang}"
        )
        btn.callback = self.create_callback
        self.add_item(btn)

    async def create_callback(self, interaction: discord.Interaction):
        # 기존에 있던 user_id in user_text_threads 검사 및 채널 중복 확인 로직 삭제 완료
        default_fmt = get_msg("text_config", self.lang, "modal_name_default", "{name}'s Thread")
        default_name = default_fmt.format(name=interaction.user.display_name)
        await interaction.response.send_modal(TextThreadModal(default_name, self.lang))

        default_fmt = get_msg("text_config", self.lang, "modal_name_default", "{name}'s Thread")
        default_name = default_fmt.format(name=interaction.user.display_name)
        await interaction.response.send_modal(TextThreadModal(default_name, self.lang))

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
        except Exception as e:
            print(f"[스레드 삭제 확인 오류] Thread ID {thread_id}: {e}")
            
    if has_changed:
        save_data()

# ---------------------------------------------------------
# 4. 공지사항 웹후크 크롤러
# ---------------------------------------------------------
def html_to_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator="\n").strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = text.split('\n')
    if len(lines) > 15:
        return '\n'.join(lines[:15]) + '\n\n... (Omitted below)'
    elif len(text) > 1500:
        return text[:1500] + '\n\n... (Omitted below)'
    return text

async def process_notices():
    print("\n🔍 공지사항 스캔 시작...")
    recent_urls = []
    target_channel = None

    categories = config.get("rules_config", {}).get("categories", {})
    for guild in bot.guilds:
        cat_name = categories.get("en")
        category = discord.utils.get(guild.categories, name=cat_name)
        if category:
            target_channel = discord.utils.get(category.text_channels, name="📢공지사항")
            if target_channel:
                break

    if target_channel:
        try:
            async for msg in target_channel.history(limit=20):
                if msg.embeds:
                    for embed in msg.embeds:
                        if embed.url: recent_urls.append(embed.url)
                if msg.content:
                    for word in msg.content.split():
                        if word.startswith("https://hybeim.oqupie.com/portal/2797/notice/"):
                            recent_urls.append(word.strip())
        except Exception as e:
            print(f"❌ 공지 채널 히스토리 수집 오류: {e}")

    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            await session.get(PORTAL_URL)
            await asyncio.sleep(1)
            async with session.get(API_LIST_URL) as resp:
                if resp.status != 200: return
                list_data = await resp.json()

            notices = list_data.get("result", {}).get("list", [])
            if not notices and isinstance(list_data.get("result"), list):
                notices = list_data.get("result")

            for notice in reversed(notices[:20]):
                notice_id = notice.get("id") or notice.get("notification_id") or notice.get("article_id")
                title = notice.get("title")
                if not notice_id: continue

                web_link = f"https://hybeim.oqupie.com/portal/2797/notice/{notice_id}"
                if web_link in recent_urls: continue

                async with session.get(f"{API_DETAIL_BASE_URL}{notice_id}?subdomain=hybeim") as d_resp:
                    if d_resp.status != 200: continue
                    detail_data = await d_resp.json()

                raw_html = detail_data.get("result", {}).get("content", "본문 없음")
                clean_text = html_to_text(raw_html)
                content = f"📢 **{title}**\n\n**Preview:**\n```\n{clean_text}\n```\n**View Details:**\n{web_link}"

                async with session.post(WEBHOOK_URL, json={"content": content}) as wh_resp:
                    if wh_resp.status in (200, 204):
                        print(f"✅ 새 공지 전송 성공: {title}")
    except Exception as e:
        print(f"❌ 공지 크롤링 수집 오류: {e}")

@bot.command(name="공지")
@commands.has_permissions(administrator=True)
async def fetch_notices_command(ctx):
    try: await ctx.message.delete()
    except Exception: pass
    await process_notices()

# ---------------------------------------------------------
# 5. 초기화 보조 함수 및 메인 이벤트 핸들러
# ---------------------------------------------------------
async def setup_rules_channel(guild):
    r_cats = config.get("rules_config", {}).get("categories", {})
    r_chans = config.get("rules_config", {}).get("channels", {})
    for lang in ["en", "zh-TW"]:
        cat = discord.utils.get(guild.categories, name=r_cats.get(lang))
        if cat:
            chan = discord.utils.get(cat.text_channels, name=r_chans.get(lang))
            if chan:
                has_msg = False
                async for m in chan.history(limit=10):
                    if m.author == bot.user:
                        has_msg = True
                        break
                if not has_msg:
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
                lang = conf["lang"]
                check_txt = get_msg("voice_config", lang, "notice_text_check")
                has_msg = False
                async for m in chan.history(limit=10):
                    if m.author == bot.user and check_txt in m.content:
                        has_msg = True
                        break
                if not has_msg:
                    notice = get_msg("voice_config", lang, "notice_text")
                    await chan.send(notice, view=VoiceChannelCreateView(lang))

async def setup_text_channel(guild):
    t_cats = config.get("text_config", {}).get("categories", {})
    for cat_name, conf in t_cats.items():
        category = discord.utils.get(guild.categories, name=cat_name)
        if category:
            chan = discord.utils.get(category.text_channels, name=conf["channel"])
            if chan:
                lang = conf["lang"]
                check_txt = get_msg("text_config", lang, "notice_text_check")
                has_msg = False
                async for m in chan.history(limit=10):
                    if m.author == bot.user and check_txt in m.content:
                        has_msg = True
                        break
                if not has_msg:
                    notice = get_msg("text_config", lang, "notice_text")
                    await chan.send(notice, view=TextChannelCreateView(lang))

@bot.event
async def on_ready():
    print(f"✅ 통합 관리 봇 로그인 완료: {bot.user}")
    
    # 1. Render 전용 간이 웹서버 가동
    await start_web_server()
    
    load_config()
    load_data()

    # 2. View 영구 등록
    for lang in ["en", "zh-TW"]:
        bot.add_view(RuleAgreementView(lang))
        bot.add_view(VoiceChannelCreateView(lang))
        bot.add_view(TextChannelCreateView(lang))
        bot.add_view(InviteButtonView(lang))

    # 3. 비공개 스레드 삭제 체크 백그라운드 루프 시작
    if not check_inactive_threads.is_running():
        check_inactive_threads.start()

    # 4. 복구: 음성 채널 데이터 검증 및 타이머 세팅
    has_cleaned_voice = False
    for channel_id, owner_id in list(voice_channel_owners.items()):
        try:
            channel = await bot.fetch_channel(channel_id)
            if len(channel.members) == 0:
                task = asyncio.create_task(delete_voice_channel_timer(channel_id, 300))
                voice_empty_tasks[channel_id] = task
        except (discord.NotFound, discord.HTTPException):
            voice_channel_owners.pop(channel_id, None)
            user_voice_channels.pop(owner_id, None)
            has_cleaned_voice = True

    if has_cleaned_voice:
        save_data()

    # 4.5. 복구: 비공개 텍스트 쓰레드 데이터 검증
    has_cleaned_text = False
    for thread_id, owner_id in list(text_thread_owners.items()):
        try:
            await bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.HTTPException):
            text_thread_owners.pop(thread_id, None)
            user_text_threads.pop(owner_id, None)
            has_cleaned_text = True

    if has_cleaned_text:
        save_data()

    # 5. 길드별 채널 메시지 및 버튼 자동 설치
    for guild in bot.guilds:
        try:
            await setup_rules_channel(guild)
            await setup_voice_channel(guild)
            await setup_text_channel(guild)
        except Exception as e:
            print(f"⚠️ [{guild.name}] 초기화 중 오류 발생: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel and len(before.channel.members) == 0:
        c_id = before.channel.id
        if c_id in voice_channel_owners:
            task = asyncio.create_task(delete_voice_channel_timer(c_id, 300))
            voice_empty_tasks[c_id] = task

    if after.channel and after.channel.id in voice_empty_tasks:
        voice_empty_tasks[after.channel.id].cancel()
        voice_empty_tasks.pop(after.channel.id, None)

# ---------------------------------------------------------
# 실행 (Render 환경 변수 BOT_TOKEN 읽기)
# ---------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "")
bot.run(TOKEN)
