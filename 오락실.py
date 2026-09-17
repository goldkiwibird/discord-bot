import asyncio
import json
import math
import os
import random
import secrets
import sys
import time
import traceback
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from urllib.parse import quote

import asyncpg
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# 윈도우 콘솔 기본 인코딩(cp949)이 이모지 등 일부 유니코드 문자를 못 그려서 print()가 죽는 걸 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()  # 로컬에 .env 파일이 있으면 읽어서 환경변수로 등록 (Render 등 배포 환경에선 .env가 없어도 그냥 무시되고 실제 환경변수를 씀)

CONFIG_FILE = "오락실.json"
KST = ZoneInfo("Asia/Seoul")
# 영웅 기본 스탯(직업 기본값 + 종족 보정)과 user_cards의 bonus_* 컬럼이 쓰는 스탯 5종
STAT_KEYS = ("attack", "hp", "defense", "accuracy", "evasion")

def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# 설정은 모듈을 읽을 때 한 번 읽어둔다 (패널 문구·PVP 규칙 등이 클래스 정의 시점부터 필요하다).
config = load_config()


# 영웅 마스터 데이터. **누구인지(id/이름/등급/종족/직업/속성/번역)는 DB의 `heroes` 테이블**에 있고,
# **수치(직업 기본값/종족 보정)는 오락실.json의 `hero_stat_config`**에 있다. 기본 스탯은 저장하지
# 않고 둘을 더해서 만든다 — 예전 Neon의 `hero_base_stats` 뷰가 하던 계산 그대로라, 직업이나
# 종족 수치를 고치면 54종에 자동 반영된다 (강화분은 user_cards의 bonus_*로 따로 더한다).
#
# **봇이 켜질 때 한 번만 읽어서 여기에 세워둔다.** 54행짜리 고정 데이터인데 Neon은 원격이라
# 조회 한 번이 왕복 ~190ms고, 화면마다 다시 물어보면 그만큼 그대로 느려진다 (소환 이력은 예전에
# 이것 때문에 왕복을 한 번 더 해서 427ms가 걸렸다). 대신 **영웅 행을 고치면 봇을 다시 켜야
# 반영된다** — 소환 로스터는 예전부터 그랬으므로 달라진 제약은 없다.
HEROES: dict[int, dict] = {}                    # id -> 영웅 (기본 스탯까지 계산된 상태)
HEROES_BY_GRADE: dict[str, list[dict]] = {}     # 소환이 쓰는 등급별 묶음


_hero_master_warned = False


def hero_master_ready() -> bool:
    """마스터 데이터가 세워져 있는지. 비어 있으면 **한 번만** 시끄럽게 알린다.

    비어 있으면 보유 카드·덱·이력이 전부 빈 화면으로 나오는데, 그게 "카드가 없다"와
    구분이 안 돼서 원인을 찾기 어렵다 (실제로 미리보기 스크립트가 이 상태로 돌다 막혔다).
    """
    global _hero_master_warned
    if HEROES:
        return True
    if not _hero_master_warned:
        _hero_master_warned = True
        print("⚠️ 영웅 마스터 데이터가 로드되지 않았습니다 (load_hero_master). "
              "카드/덱/이력이 비어 보입니다.")
    return False


def hero_base_stats(job: str, race: str) -> dict[str, int]:
    """직업 기본값 + 종족 보정 = 그 영웅의 기본 스탯."""
    conf = config["hero_stat_config"]
    job_stats = conf["job_base_stats"][job]
    race_bonus = conf["race_stat_bonus"][race]
    return {key: job_stats[key] + race_bonus[key] for key in STAT_KEYS}


async def load_hero_master(conn) -> int:
    """`heroes`를 읽어 HEROES / HEROES_BY_GRADE를 채운다. 읽은 영웅 수를 돌려준다.

    설정에 없는 직업·종족이 섞여 있으면 그 영웅만 건너뛰고 경고한다 — 통째로 죽으면
    출석·포인트까지 같이 멈추기 때문이다.
    """
    HEROES.clear()
    HEROES_BY_GRADE.clear()
    try:
        rows = await conn.fetch(
            "SELECT id, name, element, grade, race, job, name_en FROM heroes")
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as e:
        print(f"⚠️ {type(e).__name__}: 소환/덱 기능이 비활성화됩니다. "
              f"hero-cards의 heroes.sql / hero_name_translations.sql을 Neon에 반영하세요.")
        return 0

    skipped = []
    for row in rows:
        hero = dict(row)
        try:
            hero.update(hero_base_stats(hero["job"], hero["race"]))
        except KeyError as missing:
            skipped.append((hero["id"], missing.args[0]))
            continue
        HEROES[hero["id"]] = hero
    if skipped:
        print(f"⚠️ 직업/종족 수치가 오락실.json에 없어 건너뛴 영웅: {skipped}")

    # 등급별 묶음. 정렬은 예전 DB(C.UTF-8 콜레이션)가 주던 순서와 같다.
    for hero in sorted(HEROES.values(), key=lambda h: (h["grade"], h["id"])):
        HEROES_BY_GRADE.setdefault(hero["grade"], []).append(hero)
    return len(HEROES)


def today_kst() -> date:
    return datetime.now(KST).date()

def resolve_lang(interaction: discord.Interaction) -> str:
    """명령어를 입력한 채널의 카테고리로 표시 언어를 정한다 (유저의 디스코드 클라이언트 언어가 아님).

    통합관리봇의 카테고리별 자동번역과 같은 방식 — 영어 채널 카테고리에서 명령어를 쓰면
    그 카테고리 언어로, 그 외(주로 한국어 채널)에서는 한국어로 응답한다.
    스레드(포럼 댓글 등)에서 실행된 경우 부모 채널의 카테고리를 본다.
    """
    channel = interaction.channel
    category = getattr(channel, "category", None)
    if category is None:
        parent = getattr(channel, "parent", None)
        category = getattr(parent, "category", None)
    category_name = getattr(category, "name", None)
    return config.get("category_language_config", {}).get(category_name, "ko")

def hero_display_name(hero: dict, lang: str) -> str:
    """카드/메시지에 실제로 노출할 영웅 이름. 해당 언어 번역이 없으면 한국어 원본으로 대체."""
    if lang.startswith("en"):
        return hero.get("name_en") or hero["name"]
    return hero["name"]

def get_msg(locale_value: str, key: str, **kwargs) -> str:
    """유저의 디스코드 클라이언트 언어(locale_value, 예: 'en-US')에 맞는 응답 문구를 반환.
    언어 자체가 없거나 그 언어에 해당 문구만 빠져 있어도 영어(en-US)로 대체
    (한국인 전용 커뮤니티가 아니므로 기본값은 영어)."""
    messages = config.get("messages", {})
    fallback = messages.get("en-US", {})
    lang_messages = messages.get(locale_value, fallback)
    template = lang_messages.get(key) or fallback.get(key, "")
    return template.format(**kwargs) if kwargs else template

async def ensure_schema(conn):
    """봇이 직접 관리하는 테이블을 만든다.

    영웅 마스터 데이터(heroes)는 hero-cards 폴더의 SQL로 따로 넣는 자료라 여기서 만들지 않는다.
    직업/종족 수치는 DB가 아니라 오락실.json의 hero_stat_config에 있다 (load_hero_master 참고).
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            points BIGINT NOT NULL DEFAULT 0,
            last_checkin_date DATE,
            checkin_streak INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # 유저가 보유한 카드. 스탯은 절대값이 아니라 강화로 올린 증가분만 저장한다.
    # 그래야 나중에 직업/종족 기본 스탯을 조정해도 기존 카드에 그대로 반영된다.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_cards (
            user_id BIGINT NOT NULL,
            hero_id INT NOT NULL,
            enhance_count INT NOT NULL DEFAULT 0,
            bonus_attack INT NOT NULL DEFAULT 0,
            bonus_hp INT NOT NULL DEFAULT 0,
            bonus_defense INT NOT NULL DEFAULT 0,
            bonus_accuracy INT NOT NULL DEFAULT 0,
            bonus_evasion INT NOT NULL DEFAULT 0,
            obtained_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, hero_id)
        )
    """)
    # 덱 편성. 한 카드는 (user_id, hero_id) UNIQUE 제약으로 여러 덱에 동시에 못 들어간다 — 규칙을 DB가 직접 보장.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_decks (
            user_id BIGINT NOT NULL,
            deck_number INT NOT NULL,
            slot INT NOT NULL,
            hero_id INT NOT NULL,
            PRIMARY KEY (user_id, deck_number, slot),
            -- 같은 카드를 여러 덱에 넣는 건 허용하고, **한 덱 안에서의 중복만** 막는다.
            -- 예전에는 UNIQUE (user_id, hero_id)라 1덱에 넣은 카드를 2덱에 못 넣었다.
            UNIQUE (user_id, deck_number, hero_id)
        )
    """)
    # 이미 만들어진 테이블에는 위 CREATE가 적용되지 않으므로 제약을 갈아끼운다.
    # 제약을 푸는 방향이라 기존 데이터는 그대로 유효하다(데이터 삭제/이동 없음).
    # 되돌리려면 덱 간 중복을 먼저 정리해야 옛 제약을 다시 걸 수 있다.
    await conn.execute("""
        DO $$
        BEGIN
            ALTER TABLE user_decks DROP CONSTRAINT IF EXISTS user_decks_user_id_hero_id_key;
            BEGIN
                ALTER TABLE user_decks
                    ADD CONSTRAINT user_decks_user_id_deck_number_hero_id_key
                    UNIQUE (user_id, deck_number, hero_id);
            EXCEPTION WHEN duplicate_table OR duplicate_object THEN
                NULL;   -- 이미 걸려 있으면 그대로 둔다 (재시작마다 안전하게 반복 실행)
            END;
        END $$;
    """)
    # PVP는 대결마다 어느 덱을 쓸지 그때그때 고르는 방식으로 가기로 해서
    # "현재 사용 중인 덱"이라는 고정 개념 자체가 필요 없어졌다 — 예전에 추가했던 컬럼 정리.
    await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS active_deck")

    # PVP 매치. 진행 중인 판의 세부 상태(고른 카드 등)는 메모리에 두고, 여기에는
    # "판돈을 누구에게서 얼마나 미리 빼뒀는지"만 남긴다. 봇이 매치 도중에 재시작되면
    # 이 기록을 보고 묶여 있던 판돈을 돌려줘야 하기 때문 (그게 없으면 포인트가 증발한다).
    await conn.execute("""
        -- 소환 1회를 1행에 담는다 (10연도 1행). 카드 1장당 1행으로 쌓으면 같은 카드 수를
        -- 저장하는 데 4배가 든다 (실측: 카드당 135.5B -> 33.9B). 행 하나하나의 고정 오버헤드
        -- (헤더 + 인덱스 엔트리)가 실제 데이터보다 크기 때문이다.
        CREATE TABLE IF NOT EXISTS summon_log (
            log_id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            hero_ids INT[] NOT NULL,
            outcomes TEXT[] NOT NULL,
            refund INT NOT NULL DEFAULT 0,
            cost INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS summon_log_user_idx ON summon_log (user_id, log_id DESC)
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pvp_matches (
            match_id BIGSERIAL PRIMARY KEY,
            challenger_id BIGINT NOT NULL,
            opponent_id BIGINT NOT NULL,
            wager BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            winner_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settled_at TIMESTAMPTZ
        )
    """)
    # 참가자 이름을 도전장 만들 때 같이 적어둔다. **디스코드에서 나중에 되물을 수 없기 때문**이다 —
    # 이 봇은 members(특권) 인텐트가 꺼져 있고, 그러면 discord.py가 store_user를
    # store_user_no_intents로 갈아끼워 **유저 캐시에 아무것도 넣지 않는다**(2.7.1 소스 확인).
    # 그래서 `bot.get_user()`는 언제 불러도 None이고, 전적이 전부 "알 수 없는 상대"로 보였다.
    await conn.execute("""
        ALTER TABLE pvp_matches
            ADD COLUMN IF NOT EXISTS challenger_name TEXT,
            ADD COLUMN IF NOT EXISTS opponent_name TEXT
    """)
    # 전적 조회와 보관 상한 정리가 둘 다 "이 사람이 낀 매치를 최근 순으로" 훑는다.
    # 한 행이 두 사람 것이라 컬럼마다 인덱스가 따로 필요하다 (OR는 BitmapOr로 합쳐진다).
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS pvp_matches_challenger_idx
            ON pvp_matches (challenger_id, match_id DESC)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS pvp_matches_opponent_idx
            ON pvp_matches (opponent_id, match_id DESC)
    """)

class ArcadeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool: asyncpg.Pool | None = None
        # 54종 고정 데이터라 시작할 때 한 번만 읽어서 등급별로 묶어둔다 (소환할 때마다 조회하지 않음)
        self.heroes_by_grade: dict[str, list[dict]] = {}

    async def setup_hook(self):
        database_url = os.environ["DATABASE_URL"]
        # **쿼리에 시간 상한을 건다.** 상한이 없으면 한 번 멈춘 쿼리가 연결 하나를 영영
        # 붙잡고, 연결이 몇 개뿐이라 그 여파가 소환·정산 같은 **다른 기능까지 번진다**.
        # 실측으로 상한에 걸려 끊긴 뒤에도 그 연결은 그대로 다시 쓸 수 있다(asyncpg가
        # 서버 쪽 쿼리를 취소하고 연결은 살려둔다).
        #
        # 연결 수를 5 -> 10으로 올린 이유: Neon은 원격이라 **한 번 왕복이 ~190ms고 그 대부분이
        # 기다리는 시간**이다(CPU가 아니다). 기다리는 동안 다른 요청이 그 연결을 못 쓰는 게
        # 병목이라, 연결을 늘리면 그만큼 겹쳐 처리된다.
        self.pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=10,
            command_timeout=config["web_config"].get("db_command_timeout_seconds", 10),
            timeout=config["web_config"].get("db_connect_timeout_seconds", 10),
        )

        async with self.pool.acquire() as conn:
            await ensure_schema(conn)
            await self.load_heroes(conn)
            # 지난번에 봇이 매치 도중에 종료됐다면 그때 묶여 있던 판돈을 여기서 돌려준다
            # (진행 중이던 판은 버튼이 이미 죽어서 이어갈 수 없으므로 취소가 맞다)
            refunded = await refund_stale_matches(conn)
            if refunded:
                print(f"↩️ 중단됐던 매치 {refunded}건의 판돈을 반환했습니다.")

        await start_web_server()

        # 기능은 전부 채널 패널 버튼으로 옮겼고 슬래시 명령은 하나도 등록하지 않는다.
        # 그래도 sync는 해야 한다 — **빈 트리를 동기화해야 예전에 등록됐던 /출석 같은 명령이
        # 디스코드에서 사라진다.** 안 하면 목록에 남아 있다가 눌러도 응답이 없다.
        dev_guild_id = os.getenv("DEV_GUILD_ID")
        if dev_guild_id:
            guild = discord.Object(id=int(dev_guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            # 전역 동기화는 디스코드 클라이언트에 반영되는 데 최대 1시간 정도 걸릴 수 있음
            await self.tree.sync()

    async def load_heroes(self, conn):
        """시작할 때 한 번 영웅 마스터 데이터를 읽어 메모리에 세운다 (소환 로스터 포함).

        화면마다 다시 조회하지 않으려고 여기서 한 번만 읽는다. **영웅 행을 고쳤으면 봇을
        다시 켜야 반영된다.**
        """
        count = await load_hero_master(conn)
        self.heroes_by_grade = {grade: list(heroes) for grade, heroes in HEROES_BY_GRADE.items()}
        if count:
            print(f"영웅 데이터 {count}종 로드: "
                  + ", ".join(f"{g} {len(v)}종" for g, v in self.heroes_by_grade.items()))

bot = ArcadeBot()

@bot.event
async def on_ready():
    print(f"✅ 오락실 봇 로그인 완료: {bot.user}")

    # on_ready는 재연결마다 다시 불릴 수 있으므로 한 번만 해야 하는 일은 가드로 막는다.
    # (패널 재게시는 매번 해도 무해하지만, View 등록은 중복되면 쌓인다.)
    global panel_views_registered
    if not panel_views_registered:
        panel_views_registered = True
        for conf in config.get("panel_config", {}).get("categories", {}).values():
            bot.add_view(ArcadePanelView(conf["lang"]))

    posted = 0
    for guild in bot.guilds:
        posted += await setup_arcade_panel(guild)
    print(f"🕹️ 오락실 패널 {posted}곳에 게시")


panel_views_registered = False

# 출석 SQL. 테스트(test_오락실.py)가 이 상수를 그대로 import해서 검증하므로, 문구를 복사해
# 옮겨 적지 말 것 — 복사본을 두면 운영 쿼리만 바뀌었을 때 테스트가 눈치채지 못한다.
# 인자: $1 user_id, $2 기본 보상, $3 오늘, $4 어제, $5 신규 유저 보상,
#       $6 연속 하루당 보너스, $7 보너스 상한.
# `xmax = 0`은 ON CONFLICT를 안 타고 INSERT된 행, 즉 DB에 없던 신규 유저라는 뜻이다.
#
# 연속 보너스는 **갱신된 연속일수 - 1**에 비례한다(1일차 0원, 2일차 +20 … 6일차부터 상한 100).
# SET 절에서는 새 값을 아직 모르므로 "이어지면 기존 streak, 끊기면 0"으로 같은 값을 만든다
# (이어질 때 새 streak = 기존 + 1 이므로 기존 streak = 새 streak - 1).
# RETURNING 절의 checkin_streak은 이미 갱신된 값이라 INSERT 분기(=1, 보너스 0)에도 그대로 맞는다.
CHECKIN_SQL = """
    INSERT INTO users (user_id, points, last_checkin_date, checkin_streak)
    VALUES ($1, $5, $3, 1)
    ON CONFLICT (user_id) DO UPDATE SET
        points = users.points + $2 + LEAST(
            $6 * CASE WHEN users.last_checkin_date = $4 THEN users.checkin_streak ELSE 0 END, $7),
        last_checkin_date = EXCLUDED.last_checkin_date,
        checkin_streak = CASE
            WHEN users.last_checkin_date = $4 THEN users.checkin_streak + 1
            ELSE 1
        END
    WHERE users.last_checkin_date IS DISTINCT FROM EXCLUDED.last_checkin_date
    RETURNING points, checkin_streak, (xmax = 0) AS is_first_time,
              LEAST($6 * GREATEST(checkin_streak - 1, 0), $7) AS streak_bonus
"""

async def do_checkin(interaction: discord.Interaction):
    """출석 처리. 패널 버튼에서 호출한다."""
    await interaction.response.defer(ephemeral=True)

    lang = resolve_lang(interaction)
    checkin_conf = config.get("checkin_config", {})
    daily_points = checkin_conf.get("daily_points", 100)
    first_time_points = checkin_conf.get("first_time_points", daily_points)
    bonus_per_day = checkin_conf.get("streak_bonus_per_day", 0)
    max_bonus = checkin_conf.get("max_streak_bonus", 0)
    today = today_kst()
    yesterday = today - timedelta(days=1)

    try:
        async with bot.pool.acquire() as conn:
            row = await conn.fetchrow(CHECKIN_SQL, interaction.user.id, daily_points,
                                      today, yesterday, first_time_points,
                                      bonus_per_day, max_bonus)
    except Exception as e:
        print(f"⚠️ 출석 처리 중 DB 오류: {e}")
        await interaction.followup.send(get_msg(lang, "checkin_error"), ephemeral=True)
        return

    if row is None:
        await interaction.followup.send(get_msg(lang, "checkin_already_done"), ephemeral=True)
        return

    # `xmax = 0`이면 ON CONFLICT를 안 타고 새 행이 들어갔다는 뜻 = DB에 없던 신규 유저.
    # (users 행을 만드는 곳은 이 출석 명령 하나뿐이라 "첫 출석"과 같은 의미다.)
    bonus = row["streak_bonus"]
    if row["is_first_time"]:
        # 첫 출석은 신규 보상만 준다 (1일차라 연속 보너스는 어차피 0이다)
        description = get_msg(lang, "checkin_first_time_desc", points=f"{first_time_points:,}")
    elif bonus:
        # 기본 보상과 연속 보너스를 나눠서 보여준다 (예: 200P = 100P + 보너스 100P)
        description = get_msg(lang, "checkin_success_bonus_desc",
                              total=f"{daily_points + bonus:,}",
                              base=f"{daily_points:,}", bonus=f"{bonus:,}")
    else:
        description = get_msg(lang, "checkin_success_desc", total=f"{daily_points:,}")

    embed = discord.Embed(
        title=get_msg(lang, "checkin_success_title"),
        description=description,
        color=discord.Color.green()
    )
    embed.add_field(name=get_msg(lang, "checkin_points_label"), value=f"{row['points']:,}P", inline=True)
    embed.add_field(name=get_msg(lang, "checkin_streak_label"), value=get_msg(lang, "checkin_streak_value", streak=row['checkin_streak']), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

async def do_points(interaction: discord.Interaction):
    """보유 포인트 조회. 패널 버튼에서 호출한다."""
    await interaction.response.defer(ephemeral=True)

    lang = resolve_lang(interaction)

    try:
        async with bot.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT points FROM users WHERE user_id = $1", interaction.user.id)
    except Exception as e:
        print(f"⚠️ 포인트 조회 중 DB 오류: {e}")
        await interaction.followup.send(get_msg(lang, "points_error"), ephemeral=True)
        return

    points = row["points"] if row else 0
    await interaction.followup.send(get_msg(lang, "points_balance", points=f"{points:,}"), ephemeral=True)

def pick_hero(heroes_by_grade: dict[str, list[dict]]) -> dict:
    """등급을 확률표대로 먼저 뽑고, 그 등급 안에서는 캐릭터를 균등하게 뽑는다."""
    rates = config["summon_config"]["grade_rates"]
    grades = [g for g in rates if heroes_by_grade.get(g)]
    grade = random.choices(grades, weights=[rates[g] for g in grades])[0]
    return random.choice(heroes_by_grade[grade])

def resolve_summons(picks: list[dict], owned: dict[int, dict]) -> tuple[list[dict], int]:
    """뽑힌 캐릭터들을 보유 상태에 순서대로 반영한다.

    owned를 그 자리에서 갱신하기 때문에 10연 소환에서 같은 카드가 여러 번 나오면
    신규 획득 → 강화 → (최대치 도달 후) 포인트 반환 순으로 자연스럽게 이어진다.
    DB 없이도 검증할 수 있도록 순수 함수로 분리했다.
    """
    summon_conf = config["summon_config"]
    outcomes = []
    refund_total = 0

    for hero in picks:
        limit = summon_conf["max_enhance"][hero["grade"]]
        card = owned.get(hero["id"])
        if card is None:
            owned[hero["id"]] = {"enhance_count": 0, "bonus": dict.fromkeys(STAT_KEYS, 0)}
            outcomes.append({"type": "new", "hero": hero, "max_enhance": limit})
        elif card["enhance_count"] < limit:
            stat = random.choice(STAT_KEYS)
            card["bonus"][stat] += 1
            card["enhance_count"] += 1
            outcomes.append({
                "type": "enhanced", "hero": hero, "stat": stat,
                "enhance_count": card["enhance_count"], "max_enhance": limit,
            })
        else:
            refund = summon_conf["duplicate_refund"][hero["grade"]]
            refund_total += refund
            outcomes.append({"type": "maxed", "hero": hero, "refund": refund, "max_enhance": limit})

    return outcomes, refund_total

async def run_summon(user_id: int, count: int, cost: int) -> tuple[list[dict] | None, dict[int, dict], int]:
    """포인트 차감부터 카드 반영까지 한 트랜잭션으로 처리.

    포인트가 모자라면 (None, {}, 현재 잔액)을 돌려준다.
    차감은 조건부 UPDATE라 명령어를 연타해도 잔액이 음수로 내려가지 않는다.
    """
    async with bot.pool.acquire() as conn:
        async with conn.transaction():
            spent = await conn.fetchrow(
                "UPDATE users SET points = points - $2 WHERE user_id = $1 AND points >= $2 RETURNING points",
                user_id, cost,
            )
            if spent is None:
                balance = await conn.fetchval("SELECT points FROM users WHERE user_id = $1", user_id)
                return None, {}, balance or 0

            rows = await conn.fetch("SELECT * FROM user_cards WHERE user_id = $1", user_id)
            owned = {
                row["hero_id"]: {
                    "enhance_count": row["enhance_count"],
                    "bonus": {key: row[f"bonus_{key}"] for key in STAT_KEYS},
                }
                for row in rows
            }

            picks = [pick_hero(bot.heroes_by_grade) for _ in range(count)]
            outcomes, refund = resolve_summons(picks, owned)

            changed = {o["hero"]["id"] for o in outcomes if o["type"] != "maxed"}
            if changed:
                await conn.executemany(
                    """
                    INSERT INTO user_cards (user_id, hero_id, enhance_count,
                        bonus_attack, bonus_hp, bonus_defense, bonus_accuracy, bonus_evasion)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (user_id, hero_id) DO UPDATE SET
                        enhance_count = EXCLUDED.enhance_count,
                        bonus_attack = EXCLUDED.bonus_attack,
                        bonus_hp = EXCLUDED.bonus_hp,
                        bonus_defense = EXCLUDED.bonus_defense,
                        bonus_accuracy = EXCLUDED.bonus_accuracy,
                        bonus_evasion = EXCLUDED.bonus_evasion
                    """,
                    [
                        (user_id, hero_id, owned[hero_id]["enhance_count"],
                         *(owned[hero_id]["bonus"][key] for key in STAT_KEYS))
                        for hero_id in changed
                    ],
                )

            # 소환 이력 기록. 소환 1회 = 1행이고 뽑은 카드는 배열에 담는다(10연도 1행).
            # 카드 지급과 같은 트랜잭션 안에 둬야 "지급됐는데 이력엔 없다"가 생기지 않는다.
            await conn.execute(
                """INSERT INTO summon_log (user_id, hero_ids, outcomes, refund, cost)
                   VALUES ($1, $2, $3, $4, $5)""",
                user_id,
                [o["hero"]["id"] for o in outcomes],
                [o["type"] for o in outcomes],
                refund, cost,
            )
            # 유저당 최근 N회만 남긴다. 상한을 두면 **운영 기간과 무관하게** 용량이 유저당
            # 고정(20회 = 약 6.6KB)되어 Neon 무료 한도 0.5GB를 넘길 일이 없다
            # (넘기면 INSERT/UPDATE/DELETE가 전부 막혀 봇 전체가 멈춘다).
            # 별도 스케줄러 없이 소환할 때마다 조금씩 정리하는 방식.
            keep = config["summon_config"].get("log_keep_per_user", 20)
            await conn.execute(
                """
                DELETE FROM summon_log
                WHERE user_id = $1 AND log_id <= (
                    SELECT log_id FROM summon_log
                    WHERE user_id = $1 ORDER BY log_id DESC OFFSET $2 LIMIT 1
                )
                """,
                user_id, keep,
            )

            points = spent["points"]
            if refund:
                points = await conn.fetchval(
                    "UPDATE users SET points = points + $2 WHERE user_id = $1 RETURNING points",
                    user_id, refund,
                )

    return outcomes, owned, points

def describe_outcomes(outcomes: list[dict], lang: str) -> str:
    lines = []
    for outcome in outcomes:
        hero = outcome["hero"]
        name = hero_display_name(hero, lang)
        if outcome["type"] == "new":
            lines.append(get_msg(lang, "summon_result_new", name=name, grade=hero["grade"]))
        elif outcome["type"] == "enhanced":
            lines.append(get_msg(
                lang, "summon_result_enhanced",
                name=name, grade=hero["grade"],
                stat=get_msg(lang, f"stat_{outcome['stat']}"),
                count=outcome["enhance_count"], max=outcome["max_enhance"],
            ))
        else:
            lines.append(get_msg(
                lang, "summon_result_maxed",
                name=name, grade=hero["grade"], refund=outcome["refund"],
            ))
    return "\n".join(lines)

# --- PVP 전투 판정 ----------------------------------------------------------
# 규칙은 전부 오락실.json의 pvp_config에 있고, 여기서는 그 표를 해석하기만 한다.
# 디스코드/DB 없이도 검증할 수 있도록 전부 순수 함수로 둔다.

def element_detail(mine: str, theirs: str) -> dict:
    """속성 상성 가산점과 **그 이유**.

    `element_bonus()`가 숫자만 돌려주던 것을 근거까지 같이 돌려주도록 나눴다 — 웹 관전 화면에서
    "왜 +4인지"를 보여주려면 판정 시점의 이유가 필요한데, 예전에는 계산하고 그냥 버렸다.
    `reason`은 문구 키(`pvp_reason_*`)라 3개 언어로 번역해 띄울 수 있다.
    """
    conf = config["pvp_config"]
    cycle = conf["element_cycle"]
    basics = set(cycle)

    if cycle.get(mine) == theirs:
        return {"bonus": conf["element_cycle_bonus"], "reason": "pvp_reason_element_cycle"}
    if mine == "light" and theirs == "dark":
        return {"bonus": conf["light_over_dark_bonus"], "reason": "pvp_reason_element_light"}
    if mine in basics and theirs == "light":
        return {"bonus": conf["basic_over_light_bonus"], "reason": "pvp_reason_element_basic"}
    if mine == "dark" and theirs in basics:
        return {"bonus": conf["dark_over_basic_bonus"], "reason": "pvp_reason_element_dark"}
    return {"bonus": 0, "reason": None}

def element_bonus(mine: str, theirs: str) -> int:
    """속성 상성 가산점. 나무>땅>구름>불>나무 순환(+4), 빛>어둠(+4),
    4원소가 빛을 상대할 때(+1), 어둠이 4원소를 상대할 때(+1). 그 외(마주보는 4원소 등)는 0."""
    return element_detail(mine, theirs)["bonus"]

def battle_stat_detail(me: dict, opp: dict) -> dict:
    """이번 대결에 내밀 수치와 **어떤 스탯을 왜 골랐는지**.

    상성표에 따라 각 진영이 (내 직업, 상대 직업)으로 자기 칸을 조회하는 구조라
    공격/방어 구분이 필요 없다. 사제전은 예외적으로 스탯 카테고리가 상대에 따라 정해진다.
    `stat`은 실제로 쓰인 스탯 키, `rule`은 상성표에 적힌 규칙 이름이다.
    """
    rule = config["pvp_config"]["job_matchup"][me["job"]][opp["job"]]

    if rule == "OWN_LOWEST":  # 사제를 상대하는 쪽은 자기 최저 스탯을 쓴다
        lowest = min(me[key] for key in STAT_KEYS)
        tied = [key for key in STAT_KEYS if me[key] == lowest]
    elif rule == "BOTH_HIGHEST":  # 사제 vs 사제는 각자 자기 최고 스탯
        highest = max(me[key] for key in STAT_KEYS)
        tied = [key for key in STAT_KEYS if me[key] == highest]
    elif rule == "MATCH_OPP_LOWEST":
        # 사제는 상대의 최저 스탯과 같은 카테고리를 쓴다.
        # 최저가 여러 개면 그중 사제 본인 수치가 가장 높은 것을 고른다 (상대 수치는 어느 쪽이든 동일).
        lowest = min(opp[key] for key in STAT_KEYS)
        candidates = [key for key in STAT_KEYS if opp[key] == lowest]
        best = max(me[key] for key in candidates)
        tied = [key for key in candidates if me[key] == best]
    else:
        tied = [rule]
    # 동점이면 STAT_KEYS 순서상 첫 번째를 쓴다 — 예전 min()/max()가 고르던 것과 같은 카드다.
    # `stats`는 **화면에서 빛낼 목록**이다: 최저/최고가 여러 개로 갈리면 전부 빛나야
    # "왜 이 수치가 나왔는지"가 보인다 (값 자체는 어느 쪽을 골라도 같다).
    stat = tied[0]
    return {"value": me[stat], "stat": stat, "stats": tied, "rule": rule}

def battle_stat_value(me: dict, opp: dict) -> int:
    """상성표에 따라 이 카드가 이번 대결에서 내밀 수치 (근거가 필요하면 battle_stat_detail)."""
    return battle_stat_detail(me, opp)["value"]

def battle_detail(me: dict, opp: dict) -> dict:
    """한쪽 카드의 최종 수치 = 상성 스탯 + 속성 가산, 그리고 그 근거 전부."""
    stat = battle_stat_detail(me, opp)
    element = element_detail(me["element"], opp["element"])
    return {
        "value": stat["value"] + element["bonus"],
        "base": stat["value"],
        "stat": stat["stat"],
        "stats": stat["stats"],     # 동점으로 걸린 스탯까지 — 화면이 전부 빛나게 한다
        "rule": stat["rule"],
        "element_bonus": element["bonus"],
        "element_reason": element["reason"],
    }

def resolve_battle(card_a: dict, card_b: dict) -> dict:
    """카드 1:1 대결. 동점은 무승부(양쪽 다 점수 없음)."""
    detail_a = battle_detail(card_a, card_b)
    detail_b = battle_detail(card_b, card_a)
    value_a, value_b = detail_a["value"], detail_b["value"]
    return {
        "value_a": value_a,
        "value_b": value_b,
        "detail_a": detail_a,     # 웹 관전 화면이 "왜 이 수치인지"를 띄우는 데 쓴다
        "detail_b": detail_b,
        "winner": "a" if value_a > value_b else "b" if value_b > value_a else None,
    }

def resolve_match(cards_a: list[dict], cards_b: list[dict]) -> dict:
    """라운드별 결과와 최종 승자. 5판 후 점수가 같으면 매치 무효(winner=None)."""
    rounds = [resolve_battle(a, b) for a, b in zip(cards_a, cards_b)]
    score_a = sum(1 for r in rounds if r["winner"] == "a")
    score_b = sum(1 for r in rounds if r["winner"] == "b")
    return {
        "rounds": rounds,
        "score_a": score_a,
        "score_b": score_b,
        "winner": "a" if score_a > score_b else "b" if score_b > score_a else None,
    }

# --- PVP 판돈 (차감/정산) -----------------------------------------------------

async def create_match(conn, challenger_id: int, opponent_id: int, wager: int,
                       challenger_name: str = "", opponent_name: str = "") -> tuple[int | None, str | None]:
    """도전장 기록만 만든다 (판돈은 아직 안 뺌 — 상대가 수락해야 뺀다).

    (match_id, None) 또는 (None, 실패사유)를 돌려준다.

    **이름을 여기서 같이 저장하는 이유**: 나중에 전적 화면에서 ID로 이름을 되찾을 방법이 없다.
    members 인텐트가 꺼져 있어 `bot.get_user()`가 항상 None이고(ensure_schema 주석 참고),
    `fetch_user()`로 물어보는 방법은 건마다 HTTP 왕복이 드는 데다 **서버 별명이 아니라 전역
    아이디**만 준다. 지금 화면에 쓰는 것과 같은 이름을 그 자리에서 적어두는 편이 정확하고 빠르다.
    대가로 **나중에 이름을 바꿔도 옛 기록은 그때 이름으로 남는다** — 전적은 지나간 일의 기록이라
    오히려 이쪽이 자연스럽다.
    """
    if challenger_id == opponent_id:
        return None, "self_challenge"
    if wager < 0:
        return None, "invalid_wager"

    # 진행 중인 매치가 이미 있으면 새로 못 건다 (한 사람이 동시에 여러 판을 못 하도록)
    busy = await conn.fetchval(
        """
        SELECT count(*) FROM pvp_matches
        WHERE status IN ('pending', 'playing')
          AND (challenger_id = ANY($1::bigint[]) OR opponent_id = ANY($1::bigint[]))
        """,
        [challenger_id, opponent_id],
    )
    if busy:
        return None, "already_in_match"

    if wager:
        balances = {
            r["user_id"]: r["points"] for r in await conn.fetch(
                "SELECT user_id, points FROM users WHERE user_id = ANY($1::bigint[])",
                [challenger_id, opponent_id])
        }
        if balances.get(challenger_id, 0) < wager:
            return None, "challenger_poor"
        if balances.get(opponent_id, 0) < wager:
            return None, "opponent_poor"

    match_id = await conn.fetchval(
        "INSERT INTO pvp_matches "
        "(challenger_id, opponent_id, wager, status, challenger_name, opponent_name) "
        "VALUES ($1, $2, $3, 'pending', $4, $5) RETURNING match_id",
        challenger_id, opponent_id, wager, challenger_name or None, opponent_name or None,
    )
    return match_id, None

class _MatchAbort(Exception):
    """수락 도중 잔액 부족을 만났을 때 트랜잭션을 통째로 되돌리기 위한 내부 신호."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason

async def accept_match(conn, match_id: int) -> str | None:
    """수락 시점에 양쪽에서 판돈을 실제로 차감해 묶어둔다. 실패 사유가 있으면 문자열."""
    try:
        async with conn.transaction():
            match = await conn.fetchrow(
                "SELECT * FROM pvp_matches WHERE match_id = $1 FOR UPDATE", match_id)
            if match is None or match["status"] != "pending":
                return "not_pending"

            wager = match["wager"]
            if wager:
                # 도전장을 보낸 뒤에 포인트를 써버렸을 수 있으니 여기서 다시 확인하며 차감한다.
                for user_id, reason in ((match["challenger_id"], "challenger_poor"),
                                        (match["opponent_id"], "opponent_poor")):
                    deducted = await conn.fetchval(
                        "UPDATE users SET points = points - $2 WHERE user_id = $1 AND points >= $2 "
                        "RETURNING points",
                        user_id, wager)
                    if deducted is None:
                        # 트랜잭션째 롤백되므로, 먼저 차감된 쪽도 자동으로 원복된다
                        raise _MatchAbort(reason)

            await conn.execute("UPDATE pvp_matches SET status = 'playing' WHERE match_id = $1", match_id)
    except _MatchAbort as abort:
        return abort.reason
    return None

async def settle_match(conn, match_id: int, winner_id: int | None) -> None:
    """묶어둔 판돈을 정산. 승자가 있으면 양쪽 판돈을 다 가져가고, 무효면 각자에게 반환."""
    async with conn.transaction():
        match = await conn.fetchrow(
            "SELECT * FROM pvp_matches WHERE match_id = $1 FOR UPDATE", match_id)
        if match is None or match["status"] != "playing":
            return  # 이미 정산됐거나 취소된 매치 (중복 정산 방지)

        wager = match["wager"]
        if wager:
            if winner_id is None:
                # 매치 무효 — 뺏기지도 얻지도 않으므로 각자 그대로 반환
                await conn.executemany(
                    "UPDATE users SET points = points + $2 WHERE user_id = $1",
                    [(match["challenger_id"], wager), (match["opponent_id"], wager)])
            else:
                await conn.execute(
                    "UPDATE users SET points = points + $2 WHERE user_id = $1", winner_id, wager * 2)

        await conn.execute(
            "UPDATE pvp_matches SET status = 'finished', winner_id = $2, settled_at = now() "
            "WHERE match_id = $1",
            match_id, winner_id)
        await trim_match_history(conn, (match["challenger_id"], match["opponent_id"]))

async def cancel_match(conn, match_id: int) -> None:
    """거부/시간초과로 끝난 매치. 이미 판돈을 빼뒀다면 그대로 돌려준다."""
    async with conn.transaction():
        match = await conn.fetchrow(
            "SELECT * FROM pvp_matches WHERE match_id = $1 FOR UPDATE", match_id)
        if match is None or match["status"] not in ("pending", "playing"):
            return

        if match["status"] == "playing" and match["wager"]:
            await conn.executemany(
                "UPDATE users SET points = points + $2 WHERE user_id = $1",
                [(match["challenger_id"], match["wager"]), (match["opponent_id"], match["wager"])])

        await conn.execute(
            "UPDATE pvp_matches SET status = 'cancelled', settled_at = now() WHERE match_id = $1",
            match_id)
        await trim_match_history(conn, (match["challenger_id"], match["opponent_id"]))

async def trim_match_history(conn, user_ids) -> None:
    """끝난 매치 중 **양쪽 모두에게** 최근 N건 밖으로 밀려난 행을 지운다.

    소환 이력과 같은 이유로 상한을 둔다 — 별도 스케줄러 없이 매치가 끝날 때마다 조금씩
    정리하면 운영 기간과 무관하게 용량이 유저당 고정된다 (Neon 무료 0.5GB를 넘기면
    INSERT/UPDATE/DELETE가 전부 막혀 봇 전체가 멈춘다).

    **소환 이력과 결정적으로 다른 점: 한 행이 두 사람의 기록이다.** 그래서 "내 최근 20건
    밖"이라는 이유만으로 지우면 아직 20건 안쪽인 상대의 전적까지 같이 사라진다. 지우는 조건은
    **두 참가자 모두에게 더 최근인 매치가 이미 N건 이상 있을 것**이다.

    진행 중(`pending`/`playing`)인 행은 판돈을 묶어둔 장부라 절대 지우지 않는다.
    """
    keep = config["pvp_config"].get("history_keep_per_user", 20)
    await conn.execute(
        """
        DELETE FROM pvp_matches m
        WHERE m.status IN ('finished', 'cancelled')
          AND (m.challenger_id = ANY($1::bigint[]) OR m.opponent_id = ANY($1::bigint[]))
          AND NOT EXISTS (
              SELECT 1
              FROM unnest(ARRAY[m.challenger_id, m.opponent_id]) AS p(uid)
              WHERE (SELECT count(*) FROM pvp_matches k
                     WHERE (k.challenger_id = p.uid OR k.opponent_id = p.uid)
                       AND k.status <> 'pending'
                       AND k.match_id > m.match_id) < $2
          )
        """,
        list(user_ids), keep,
    )

async def refund_stale_matches(conn) -> int:
    """봇이 매치 도중에 죽었다 살아난 경우, 묶여 있던 판돈을 전부 돌려준다.

    진행 중이던 판은 버튼(View)이 이미 죽어서 이어갈 수 없으므로 취소 처리하는 게 맞다.
    """
    stale = await conn.fetch(
        "SELECT match_id FROM pvp_matches WHERE status IN ('pending', 'playing')")
    for row in stale:
        await cancel_match(conn, row["match_id"])
    return len(stale)

# --- 덱 편성 ---------------------------------------------------------------

def validate_deck(hero_ids: list, owned_ids: set, deck_number: int) -> str | None:
    """덱 저장 요청을 검증한다. 문제가 없으면 None, 있으면 사유 문자열을 돌려준다.

    브라우저에서 오는 요청이라 클라이언트 검증은 우회될 수 있다 — 저장 직전에 서버가 다시 확인한다.
    한 덱 안에서 같은 카드를 두 번 넣는 것만 막는다 — 1덱에 넣은 카드를 2덱에 넣는 건 허용한다.
    (DB의 UNIQUE (user_id, deck_number, hero_id)가 최종적으로 막지만, 사용자에게 이유를 알려주려면 여기서도 확인)
    """
    deck_conf = config["deck_config"]
    if not (1 <= deck_number <= deck_conf["deck_count"]):
        return "invalid_deck_number"
    if len(hero_ids) != deck_conf["deck_size"]:
        return "wrong_size"
    if len(set(hero_ids)) != len(hero_ids):
        return "duplicate_in_deck"
    if not set(hero_ids) <= owned_ids:
        return "not_owned"
    return None

async def fetch_deck_page_data(conn, user_id: int, lang: str) -> dict:
    """덱 편성 화면에 필요한 데이터 (보유 카드 + 현재 덱 배치)."""
    hero_master_ready()
    rows = await conn.fetch(
        """
        SELECT c.hero_id, c.enhance_count,
               c.bonus_attack, c.bonus_hp, c.bonus_defense, c.bonus_accuracy, c.bonus_evasion,
               COALESCE((SELECT array_agg(d.deck_number ORDER BY d.deck_number)
                         FROM user_decks d
                         WHERE d.user_id = c.user_id AND d.hero_id = c.hero_id),
                        '{}') AS deck_numbers
        FROM user_cards c
        WHERE c.user_id = $1
        """,
        user_id,
    )
    # 영웅 정보는 파일에 있으므로 조인하지 않는다. 정렬은 예전 `ORDER BY h.grade, h.id`와 같다
    # (DB 콜레이션이 C.UTF-8이라 파이썬 문자열 정렬과 바이트 순서가 동일하다).
    rows = sorted((row for row in rows if row["hero_id"] in HEROES),
                  key=lambda row: (HEROES[row["hero_id"]]["grade"], row["hero_id"]))

    cards = []
    for row in rows:
        hero = HEROES[row["hero_id"]]
        bonus = {key: row[f"bonus_{key}"] for key in STAT_KEYS}
        cards.append({
            "hero_id": row["hero_id"],
            "name": hero_display_name(hero, lang),
            "image_name": hero["name"],  # 이미지 파일명은 항상 한글 원본
            "grade": hero["grade"],
            "job": hero["job"],
            "element": hero["element"],  # 필터용 (속성/등급/직업)
            # 보유 카드 화면이라 강화분이 반영된 현재 스탯 + 증가분을 같이 내려준다 ("10 (+3)" 표기용)
            "stats": {key: hero[key] + bonus[key] for key in STAT_KEYS},
            "bonus": bonus,
            "enhance_count": row["enhance_count"],
            # 같은 카드가 여러 덱에 들어갈 수 있으므로 목록으로 내려준다.
            # LEFT JOIN으로 두면 덱 수만큼 행이 늘어나 같은 카드가 화면에 여러 번 뜬다.
            "deck_numbers": list(row["deck_numbers"]),
        })

    return {"cards": cards}

async def fetch_summon_history(conn, user_id: int, lang: str, limit: int) -> list[dict]:
    """소환 이력. 최근 것부터 `limit`건 (1건 = 소환 1회, 10연이면 카드 10장이 한 건에 들어 있음).

    영웅 이름/등급은 로그에 복사해 넣지 않고 마스터 데이터(`HEROES`)에서 조회한다 — 복사해 두면
    나중에 마스터 데이터를 고쳤을 때 과거 기록만 옛 값으로 남아 어긋난다.
    `bot.heroes_by_grade`(봇 인스턴스의 로스터)가 아니라 모듈 상수를 보는 이유는 봇이 켜진
    상태에만 의존하지 않기 위함이다 — 그래야 이 함수를 단독으로 검증할 수 있고, 로스터가
    비어 있어서 카드가 조용히 사라지는 일도 없다 (실제로 테스트가 그걸 잡은 적이 있다).
    """
    rows = await conn.fetch(
        """
        SELECT log_id, hero_ids, outcomes, refund, cost, created_at
        FROM summon_log
        WHERE user_id = $1
        ORDER BY log_id DESC
        LIMIT $2
        """,
        user_id, limit,
    )
    if not rows:
        return []

    history = []
    for row in rows:
        cards = []
        for hero_id, outcome in zip(row["hero_ids"], row["outcomes"]):
            hero = HEROES.get(hero_id)
            if hero is None:      # 마스터 데이터에서 빠진 영웅 — 이름을 못 찾아도 기록은 보여준다
                cards.append({"name": f"#{hero_id}", "image_name": "", "grade": "R",
                              "job": "warrior", "element": "", "outcome": outcome,
                              "stats": {key: 0 for key in STAT_KEYS}})
                continue
            cards.append({
                "name": hero_display_name(hero, lang),
                "image_name": hero["name"],   # 이미지 파일명은 항상 한글 원본
                "grade": hero["grade"],
                "job": hero["job"],           # 웹에서 카드 그림을 조립할 때 직업/속성 아이콘이 필요하다
                "element": hero["element"],
                # **강화를 반영하지 않은 기본 스탯**이다. 소환 결과 카드는 "그때 뽑은 카드"를
                # 보여주는 자리라, 나중에 강화한 수치를 얹으면 기록이 아니라 현재 상태가 된다.
                "stats": {key: hero[key] for key in STAT_KEYS},
                "outcome": outcome,           # new / enhance / maxed
            })
        history.append({
            "log_id": row["log_id"],
            "cards": cards,
            "refund": row["refund"],
            "cost": row["cost"],
            "pulled_at": row["created_at"].isoformat(),
        })
    return history

async def fetch_match_history(conn, user_id: int, limit: int) -> list[dict]:
    """PVP 전적. 최근 것부터 `limit`건.

    `pvp_matches`에 이미 쌓여 있어서 새 테이블이 필요 없다. 상대 이름도 도전장을 만들 때 같이
    적어둔 것을 그대로 읽는다 — 디스코드에 되물을 수 없기 때문이다(create_match 주석 참고).
    이름 칸이 생기기 전의 옛 기록은 비어 있고, 그건 화면에서 "알 수 없는 상대"로 적는다.
    """
    rows = await conn.fetch(
        """
        SELECT match_id, challenger_id, opponent_id, wager, status, winner_id, created_at,
               challenger_name, opponent_name
        FROM pvp_matches
        WHERE (challenger_id = $1 OR opponent_id = $1)
          AND status <> 'pending'
        ORDER BY match_id DESC
        LIMIT $2
        """,
        user_id, limit,
    )

    history = []
    for row in rows:
        me_is_challenger = row["challenger_id"] == user_id
        them = row["opponent_id"] if me_is_challenger else row["challenger_id"]
        them_name = row["opponent_name"] if me_is_challenger else row["challenger_name"]
        if row["status"] != "finished":
            result = "cancelled"          # 취소/무효 — 판돈은 반환된 상태
        elif row["winner_id"] is None:
            result = "draw"
        else:
            result = "win" if row["winner_id"] == user_id else "loss"
        # 판돈전만 득실이 있다. 승리는 +판돈, 패배는 -판돈, 무승부/취소는 0.
        delta = 0
        if result == "win":
            delta = row["wager"]
        elif result == "loss":
            delta = -row["wager"]
        history.append({
            "match_id": row["match_id"],
            "opponent_id": str(them),     # 자바스크립트 Number는 디스코드 ID(64비트)를 못 담는다
            "opponent_name": them_name or "",
            "result": result,
            "wager": row["wager"],
            "delta": delta,
            "played_at": row["created_at"].isoformat(),
        })
    return history

async def fetch_battle_decks(conn, user_id: int, lang: str) -> dict[int, list[dict]]:
    """PVP에 쓸 수 있는 덱(5장이 다 채워진 것)만 카드 정보까지 붙여서 가져온다."""
    hero_master_ready()
    rows = await conn.fetch(
        """
        SELECT d.deck_number, d.slot, d.hero_id,
               c.bonus_attack, c.bonus_hp, c.bonus_defense, c.bonus_accuracy, c.bonus_evasion
        FROM user_decks d
        JOIN user_cards c ON c.user_id = d.user_id AND c.hero_id = d.hero_id
        WHERE d.user_id = $1
        ORDER BY d.deck_number, d.slot
        """,
        user_id,
    )

    decks: dict[int, list[dict]] = {}
    for row in rows:
        hero = HEROES.get(row["hero_id"])
        if hero is None:      # 마스터 데이터에서 빠진 영웅 — 덱이 5장을 못 채워 제외된다
            continue
        # PVP는 유저가 실제로 보유한 카드로 싸우므로 강화분이 반영된 현재 스탯을 쓴다
        card = {
            "hero_id": row["hero_id"],
            "name": hero["name"],  # 이미지 파일명용 한글 원본
            "display_name": hero_display_name(hero, lang),
            "grade": hero["grade"],
            "job": hero["job"],
            "element": hero["element"],
            "bonus": {key: row[f"bonus_{key}"] for key in STAT_KEYS},
        }
        card.update({key: hero[key] + card["bonus"][key] for key in STAT_KEYS})
        decks.setdefault(row["deck_number"], []).append(card)

    deck_size = config["deck_config"]["deck_size"]
    return {number: cards for number, cards in decks.items() if len(cards) == deck_size}

async def save_deck(conn, user_id: int, deck_number: int, hero_ids: list) -> str | None:
    """덱을 통째로 교체 저장. 실패 사유가 있으면 문자열, 성공이면 None."""
    async with conn.transaction():
        owned = {r["hero_id"] for r in
                 await conn.fetch("SELECT hero_id FROM user_cards WHERE user_id = $1", user_id)}
        reason = validate_deck(hero_ids, owned, deck_number)
        if reason:
            return reason

        # 다른 덱에 이미 들어간 카드여도 상관없다 — 덱 간 중복은 허용하고
        # 한 덱 안에서의 중복만 막는다(validate_deck의 duplicate_in_deck + DB의 UNIQUE 제약).
        await conn.execute("DELETE FROM user_decks WHERE user_id = $1 AND deck_number = $2", user_id, deck_number)
        await conn.executemany(
            "INSERT INTO user_decks (user_id, deck_number, slot, hero_id) VALUES ($1, $2, $3, $4)",
            [(user_id, deck_number, slot, hero_id) for slot, hero_id in enumerate(hero_ids, start=1)],
        )
    return None

async def reset_card_enhance(conn, user_id: int, hero_id: int) -> str | None:
    """카드 한 장의 강화(증가분 스탯 + 강화 횟수)만 초기화한다. 포인트 환불은 없음.

    덱 배치(user_decks)는 건드리지 않는다 — 어느 덱에 들어있든 그대로 두고 스탯만 기본치로 되돌린다.
    """
    updated = await conn.fetchval(
        """
        UPDATE user_cards SET enhance_count = 0,
            bonus_attack = 0, bonus_hp = 0, bonus_defense = 0, bonus_accuracy = 0, bonus_evasion = 0
        WHERE user_id = $1 AND hero_id = $2
        RETURNING hero_id
        """,
        user_id, hero_id,
    )
    return None if updated is not None else "not_owned"

async def summon_and_reply(interaction: discord.Interaction, count: int, cost: int):
    """/소환과 /10소환이 공유하는 처리 흐름. 뽑는 개수와 비용만 다르다."""
    await interaction.response.defer(ephemeral=True)
    lang = resolve_lang(interaction)

    if not bot.heroes_by_grade:
        await interaction.followup.send(get_msg(lang, "summon_no_heroes"), ephemeral=True)
        return

    try:
        outcomes, owned, points = await run_summon(interaction.user.id, count, cost)
    except Exception as e:
        print(f"⚠️ 소환 처리 중 DB 오류: {e}")
        await interaction.followup.send(get_msg(lang, "summon_error"), ephemeral=True)
        return

    if outcomes is None:
        await interaction.followup.send(get_msg(lang, "summon_insufficient", cost=cost, points=f"{points:,}"), ephemeral=True)
        return

    embed = discord.Embed(
        title=get_msg(lang, "summon_result_title"),
        description=describe_outcomes(outcomes, lang) + "\n\n" + get_msg(lang, "summon_points_left", points=f"{points:,}"),
        color=discord.Color.gold(),
    )

    # 카드 그림은 디스코드에 붙이지 않고 웹 이력 화면에서 보여준다.
    # 서버에서 PNG를 합성하면 0.1 CPU인 Render에서 10연 기준 1초 넘게 잡아먹는데(실측),
    # 웹은 브라우저가 그리므로 서버 비용이 사실상 0이고 확대/필터 같은 것도 공짜로 얻는다.
    # 링크 전용 View는 custom_id가 없어 discord.py의 view store에 남지 않으므로
    # timeout=None을 줘도 누수가 없고, 소환마다 타이머 태스크가 생기지도 않는다 (2.7.1 소스로 확인).
    token = issue_deck_token(interaction.user.id, lang)
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.link,
        label=get_msg(lang, "summon_view_history"),
        url=f"{web_base_url()}/deck?token={token}#summons",
    ))

    # 소환 결과는 DM이 아니라 명령어를 실행한 채널에 본인에게만 보이는(ephemeral) 메시지로 전달
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# --- 덱 편성 웹페이지 -------------------------------------------------------

# 발급한 링크 토큰: {토큰: (user_id, lang, 만료시각)}.
# 몇 분짜리 단기 토큰이라 재시작 시 사라져도 문제없어서 메모리에만 둔다 (Render 디스크는 어차피 휘발성).
deck_tokens: dict[str, tuple[int, str, float]] = {}

def issue_deck_token(user_id: int, lang: str) -> str:
    ttl = config["web_config"]["token_ttl_seconds"]
    now = time.time()
    # 만료된 토큰은 이때 같이 정리 (따로 청소 작업을 돌릴 만큼 쌓이지 않음)
    for expired in [t for t, (_, _, exp) in deck_tokens.items() if exp <= now]:
        del deck_tokens[expired]

    token = secrets.token_urlsafe(24)
    deck_tokens[token] = (user_id, lang, now + ttl)
    return token

def resolve_deck_token(request) -> tuple[int, str] | None:
    entry = deck_tokens.get(request.query.get("token", ""))
    if entry is None:
        return None
    user_id, lang, expires_at = entry
    if expires_at <= time.time():
        return None
    return user_id, lang

async def handle_deck_page(request):
    resolved = resolve_deck_token(request)
    if resolved is None:
        return web.Response(text=get_msg("en-US", "web_expired"), status=403)
    return html_page("deck_page.html")

async def handle_deck_data(request):
    resolved = resolve_deck_token(request)
    if resolved is None:
        return web.json_response({"error": "expired"}, status=403)
    user_id, lang = resolved

    async with bot.pool.acquire() as conn:
        data = await fetch_deck_page_data(conn, user_id, lang)

    deck_conf = config["deck_config"]
    data["deck_count"] = deck_conf["deck_count"]
    data["deck_size"] = deck_conf["deck_size"]
    data["image_base_url"] = config["card_config"]["image_base_url"]
    data["image_ext"] = config["card_config"].get("image_ext", ".webp")
    data["stat_order"] = list(config["card_config"]["stat_order"])
    data["grades"] = ["R", "R+", "SR", "SSR"]
    # 두 이력 화면 모두 보관 상한이 있다. 화면에 적어두지 않으면 옛 기록이 사라진 것을
    # 버그로 오해한다 (상한이 있는 이유는 trim_match_history 참고).
    data["history_keep"] = config["pvp_config"].get("history_keep_per_user", 20)
    data["summons_keep"] = config["summon_config"].get("log_keep_per_user", 20)
    data["jobs"] = list(config["card_config"]["job_order"])
    data["elements"] = list(config["card_config"]["element_order"])
    data["messages"] = {
        key: get_msg(lang, key)
        for key in ("web_title", "web_deck_tab", "web_selected_count", "web_save", "web_saved",
                    "web_save_failed", "web_in_other_deck", "web_no_cards", "web_no_matches", "web_full",
                    "web_filter_all", "web_filter_element", "web_filter_grade", "web_filter_job",
                    "web_reset_enhance", "web_reset_enhance_confirm", "web_reset_enhance_done",
                    "web_reset_enhance_failed",
                    "web_tab_deck", "web_tab_history", "web_history_empty", "web_history_loading",
                    "web_result_win", "web_result_loss", "web_result_draw", "web_result_cancelled",
                    "web_history_opponent", "web_history_unknown", "web_history_friendly",
                    "web_tab_summons", "web_summons_empty", "web_summon_new",
                    "web_summon_enhance", "web_summon_maxed", "web_summon_refund",
                    "web_summon_count", "web_summon_cost",
                    "web_keep_note", "web_expired")
    }
    data["stat_labels"] = {key: get_msg(lang, f"stat_{key}") for key in STAT_KEYS}
    data["job_labels"] = {key: get_msg(lang, f"job_{key}") for key in data["jobs"]}
    data["element_labels"] = {key: get_msg(lang, f"element_{key}") for key in data["elements"]}
    return web.json_response(data)

async def handle_match_history(request):
    """덱 편성 페이지의 'PVP 전적' 탭이 부르는 API.

    탭을 눌렀을 때만 호출된다(지연 로딩) — 덱만 보고 나가는 사람에게는 조회 비용이 0이다.
    """
    resolved = resolve_deck_token(request)
    if resolved is None:
        return web.json_response({"error": "expired"}, status=403)
    user_id, lang = resolved

    limit = config["web_config"].get("history_limit", 50)
    async with bot.pool.acquire() as conn:
        matches = await fetch_match_history(conn, user_id, limit)

    # 이름은 fetch_match_history가 DB에서 같이 읽어온다. 예전에는 여기서 `bot.get_user()`로
    # 캐시를 뒤졌는데, members 인텐트가 꺼진 이 봇에서는 **그 캐시가 영영 비어 있어**
    # 전적이 전부 "알 수 없는 상대"로 나왔다 (create_match 주석 참고).
    return web.json_response({"matches": matches})

async def handle_summon_history(request):
    """덱 편성 페이지의 '소환 이력' 탭이 부르는 API (탭을 처음 열 때만 호출)."""
    resolved = resolve_deck_token(request)
    if resolved is None:
        return web.json_response({"error": "expired"}, status=403)
    user_id, lang = resolved

    limit = config["web_config"].get("history_limit", 50)
    async with bot.pool.acquire() as conn:
        summons = await fetch_summon_history(conn, user_id, lang, limit)
    return web.json_response({"summons": summons})

async def handle_deck_save(request):
    resolved = resolve_deck_token(request)
    if resolved is None:
        return web.json_response({"error": "expired"}, status=403)
    user_id, _ = resolved

    body = await request.json()
    deck_number = body.get("deck_number")
    hero_ids = body.get("hero_ids")
    if not isinstance(deck_number, int) or not isinstance(hero_ids, list):
        return web.json_response({"error": "bad_request"}, status=400)

    async with bot.pool.acquire() as conn:
        reason = await save_deck(conn, user_id, deck_number, hero_ids)
    if reason:
        return web.json_response({"error": reason}, status=400)
    return web.json_response({"ok": True})

async def handle_deck_reset_enhance(request):
    resolved = resolve_deck_token(request)
    if resolved is None:
        return web.json_response({"error": "expired"}, status=403)
    user_id, _ = resolved

    body = await request.json()
    hero_id = body.get("hero_id")
    if not isinstance(hero_id, int):
        return web.json_response({"error": "bad_request"}, status=400)

    async with bot.pool.acquire() as conn:
        reason = await reset_card_enhance(conn, user_id, hero_id)
    if reason:
        return web.json_response({"error": reason}, status=400)
    return web.json_response({"ok": True})

async def handle_health(request):
    """UptimeRobot이 주기적으로 찔러보는 상태 확인 엔드포인트.

    `/`는 프로세스가 떠 있기만 하면 무조건 200을 준다. 그래서 **봇이 디스코드 게이트웨이와
    끊기거나 DB가 죽은 채로 껍데기만 돌고 있는 상태(명령어가 전부 안 먹는 상태)를 못 잡는다.**
    여기서는 실제로 쓸 수 있는 상태인지 확인하고, 아니면 503을 줘서 UptimeRobot 알림이 울리게 한다.
    Render 스핀다운을 막는 효과는 `/`와 똑같으니 모니터는 이쪽을 보게 하는 게 이득이다.

    DB 조회에 타임아웃을 거는 이유: DB가 응답을 안 하면 이 요청이 영영 안 끝나고,
    그러면 UptimeRobot 쪽에서는 "죽었다"가 아니라 그냥 느린 것처럼 보여 알림이 늦어진다.

    **이 핸들러는 어떤 경우에도 예외를 밖으로 던지지 않는다.** 예외가 나가면 aiohttp가 500을
    주는데, 그러면 UptimeRobot 화면에서 "봇이 아픈 것"과 "상태 확인 코드 자체가 깨진 것"을
    구분할 수 없다 (설정 키 하나가 없어서 500이 나고 계속 빨간불이 뜬 적이 있다). 대신 503과
    함께 예외 타입 이름을 돌려줘서 로그를 안 봐도 원인을 좁힐 수 있게 한다 — 예외 메시지 전문은
    접속 문자열 같은 게 섞일 수 있으므로 공개 엔드포인트에는 타입 이름만 싣는다
    (전체 스택은 Render 로그로 보낸다).
    """
    try:
        # 설정에 키가 없어도 상태 확인 자체는 돌아가야 하므로 기본값을 둔다.
        # 코드보다 JSON이 늦게 배포되는 상황에서 이 엔드포인트까지 같이 죽으면 안 된다.
        timeout = config["web_config"].get("health_db_timeout_seconds", 3)

        async def probe_db():
            async with bot.pool.acquire() as conn:
                return await conn.fetchval("SELECT 1")

        database, db_ms = False, None
        if bot.pool is not None:
            started = time.perf_counter()
            try:
                database = await asyncio.wait_for(probe_db(), timeout) == 1
            except (asyncpg.PostgresError, OSError, asyncio.TimeoutError):
                database = False
            db_ms = round((time.perf_counter() - started) * 1000)

        discord_ok = bot.is_ready() and not bot.is_closed()
        healthy = discord_ok and database
        return web.json_response(
            {
                "status": "ok" if healthy else "degraded",
                "discord": discord_ok,
                "database": database,
                # 연결 전에는 latency가 NaN이라 그대로 넣으면 JSON으로 직렬화할 수 없다
                "latency_ms": None if math.isnan(bot.latency) else round(bot.latency * 1000),
                # **DB가 느린 것과 연결이 모자란 것을 구분하려고** 같이 싣는다.
                # db_ms만 크면 DB가 느린 것이고, idle이 0인 채로 db_ms가 크면 연결이 모자란 것이다.
                "db_ms": db_ms,
                "db_pool": (None if bot.pool is None else
                            {"size": bot.pool.get_size(), "idle": bot.pool.get_idle_size(),
                             "max": bot.pool.get_max_size()}),
                "rooms": len(rooms),
                "online": len(online_user_ids()),
            },
            status=200 if healthy else 503,
        )
    except Exception as e:
        traceback.print_exc()
        return web.json_response({"status": "error", "error": type(e).__name__}, status=503)

def resolve_room_view(request) -> "tuple[Room, dict] | None":
    """요청이 가리키는 방과 '보는 사람'을 찾는다.

    **방은 토큰이 있어야 볼 수 있다.** 예전 관전 링크는 토큰이 없어도 열렸는데(주소만 알면
    누구나), 방에는 비공개·추방·초대 같은 개념이 생겨서 누가 보고 있는지 알아야 한다.
    """
    entry = resolve_lobby_token(request)
    if entry is None:
        return None
    try:
        room_id = int(request.query.get("id", ""))
    except ValueError:
        return None
    room = rooms.get(room_id)
    if room is None or room.closed or entry["user_id"] not in room.members:
        return None
    return room, entry


# 화면 파일에는 **캐시 금지 헤더를 붙인다.** 안 붙이면 헤더가 하나도 없어 브라우저가 나름의
# 기준으로 캐시하는데, 그러면 **배포를 해도 옛 화면이 계속 돌아간다** — 고친 줄 알았는데 증상이
# 그대로인, 원인 찾기가 가장 성가신 형태의 사고가 난다. 페이지는 몇십 KB뿐이라 아낄 것도 없다.
NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def html_page(filename: str) -> web.Response:
    with open(filename, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html", headers=NO_CACHE)


async def handle_room_page(request):
    """방 화면. 대기실과 대결이 같은 페이지다 — 시작하면 화면만 바뀐다."""
    return html_page("match_page.html")


async def handle_lobby_page(request):
    return html_page("lobby_page.html")


async def handle_room_state(request):
    """SSE가 막혔을 때 쓰는 폴링용 단발 조회 (브라우저가 자동으로 이쪽으로 내려온다)."""
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    room.polled[entry["user_id"]] = time.time()
    return web.json_response(room.snapshot(entry["user_id"]))


# 마지막으로 목록을 받아간 시각 (user_id -> 유닉스 시각). 새로고침 연타를 막는다.
_lobby_reads: dict[int, float] = {}


async def handle_lobby_state(request):
    """방 목록 + 접속자. **첫 입장과 새로고침에만 부른다.**

    서버에서도 간격을 강제한다 — 화면 쪽 제한만 두면 주소를 직접 두드려 우회할 수 있고,
    이 조회가 로비에서 가장 비싼 작업이라 연타되면 그대로 CPU를 먹는다.
    """
    entry = resolve_lobby_token(request)
    if entry is None:
        return web.json_response({"error": "expired"}, status=403)

    user_id = entry["user_id"]
    touch_lobby(user_id)
    cooldown = config["lobby_config"].get("refresh_cooldown_seconds", 5)
    last = _lobby_reads.get(user_id)
    now = time.time()
    if last is not None and now - last < cooldown:
        return web.json_response({"error": "too_soon", "retry_after": round(cooldown - (now - last), 1)},
                                 status=429)
    _lobby_reads[user_id] = now
    return web.json_response(lobby_snapshot(user_id))


async def handle_room_invitable(request):
    """방에서 초대할 수 있는 사람들 (로비에 있고 이 방에 없는 사람).

    목록 조회(`/api/lobby`)와 따로 둔 이유: 그쪽은 새로고침 간격 제한이 걸려 있어서,
    새로고침 직후 초대 창을 열면 막힌다. 여기서는 사람 목록만 주므로 훨씬 가볍기도 하다.
    """
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, _ = resolved
    seen: dict[int, str] = {}
    for session in lobby_sessions:
        if session.user_id not in room.members and session.user_id not in room.banned:
            seen.setdefault(session.user_id, session.name)
    return web.json_response({"users": [{"id": str(uid), "name": name} for uid, name in seen.items()]})


async def handle_match_labels(request):
    """대전 화면이 처음 한 번만 받아가는 정적 자료 (문구·라벨·이미지 주소).

    상태 스냅샷에 같이 넣으면 이벤트마다 1.5KB씩 따라다니므로 따로 뺐다.
    **언어는 방이 아니라 보는 사람 기준**이다 — 한 방에 여러 언어 사용자가 같이 들어온다.
    """
    entry = resolve_lobby_token(request)
    if entry is None:
        return web.json_response({"error": "expired"}, status=403)
    lang = entry["lang"]

    return web.json_response({
        "image_base_url": config["card_config"]["image_base_url"],
        "image_ext": config["card_config"].get("image_ext", ".webp"),
        "stat_order": list(config["card_config"]["stat_order"]),
        "stat_labels": {key: get_msg(lang, f"stat_{key}") for key in STAT_KEYS},
        "job_labels": {key: get_msg(lang, f"job_{key}")
                       for key in config["card_config"]["job_order"]},
        "element_labels": {key: get_msg(lang, f"element_{key}")
                           for key in config["card_config"]["element_order"]},
        "round_timeout": config["pvp_config"]["round_pick_timeout_seconds"],
        "messages": {
            key: get_msg(lang, key)
            for key in (
                "match_vs", "match_waiting_deck", "match_waiting_pick", "match_opponent_deck",
                "web_match_spectators", "web_match_pick_prompt", "web_match_your_turn",
                "web_match_round", "web_match_win", "web_match_lose",
                "web_match_deck_prompt", "web_match_deck_label",
                "match_cancelled_timeout", "match_cancelled_error",
                "web_match_draw", "web_match_final_win", "web_match_final_lose",
                "web_match_final_draw", "web_match_lost_connection",
                "web_match_not_found", "web_match_wager",
                "pvp_reason_own_lowest", "pvp_reason_both_highest", "pvp_reason_match_opp_lowest",
                "pvp_reason_direct", "pvp_reason_element_cycle", "pvp_reason_element_light",
                "pvp_reason_element_basic", "pvp_reason_element_dark",
                "web_match_element_bonus", "web_match_rematch", "web_match_scoreline",
                # 방 대기실
                "web_room_waiting_title", "web_room_ready_title", "web_room_empty_slot",
                "web_room_ready", "web_room_unready", "web_room_ready_done",
                "web_room_start", "web_room_start_hint", "web_room_start_wait",
                "web_room_leave", "web_room_invite", "web_room_kick", "web_room_kick_confirm",
                "web_room_spectators", "web_room_spectators_empty",
                "web_room_invite_title", "web_room_invite_empty", "web_room_invite_sent",
                "web_room_became_host", "web_room_became_opponent", "web_room_kicked",
                "web_room_joined_poor", "web_room_left_notice", "web_room_seat", "web_room_host_badge",
                "web_room_sit", "web_room_stand", "web_room_stood", "web_room_demoted_poor",
                "web_room_seat_taken", "web_room_in_match", "web_room_ready_all",
                "web_room_closed", "web_room_gone_restart", "web_room_poor", "web_room_no_deck",
                "web_room_back_to_lobby", "web_room_host", "web_room_guest", "web_room_wager",
                "web_lobby_kicked_elsewhere", "web_room_closed_idle",
                "web_room_last", "web_room_last_win", "web_room_last_draw",
                "web_room_last_cancelled",
                "web_room_friendly",
                "web_room_private", "web_lobby_expired",
            )
        },
    })


async def handle_room_stream(request):
    """방 상태를 실시간으로 밀어주는 SSE 스트림 (대기실과 대결이 같은 스트림이다).

    Cloudflare/nginx가 응답을 모아뒀다 내보내면 실시간성이 깨지므로 `X-Accel-Buffering: no`를
    붙이고, 유휴 연결이 끊기지 않도록 주기적으로 주석 하트비트(`: ping`)를 보낸다.
    그래도 막히는 환경이 있을 수 있어 클라이언트는 폴링으로 내려갈 수 있게 해뒀다.

    **스트림이 끊기면 방에서 뺀다**(`drop_disconnected`). 창을 닫거나 연결이 끊긴 사람을
    그대로 두면 자리와 방이 계속 묶여서, 아무도 없는 방이 목록을 채우고 접속자 수도 부풀려진다.
    다만 새로고침도 한 번 끊기는 것이라 **몇 초 유예**를 두고, 대결 중이면 그 판이 끝난 뒤에 뺀다.

    **같은 사람의 창은 하나만 유지한다** — 새 창이 붙으면 옛 창을 끊는다. 탭마다 스트림과
    대기열이 생겨 접속자 수와 메모리가 사람 수보다 부풀기 때문이다.
    """
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    viewer_id = entry["user_id"]

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    await response.prepare(request)

    # 같은 사람의 **다른 페이지** 연결만 끊는다 (같은 페이지의 재연결은 그냥 교체)
    cid = client_id(request)
    close_other_streams(viewer_id, cid, "room")
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    live_streams[viewer_id] = (cid, queue, request)
    room.subscribers.add(queue)
    room.viewers[viewer_id] = room.viewers.get(viewer_id, 0) + 1
    if room.match is not None:
        room.match.subscribers.add(queue)

    heartbeat = config["pvp_config"].get("stream_heartbeat_seconds", 15)
    tick = min(2, heartbeat)          # 끊김을 빨리 알아채려고 대기를 짧게 끊는다
    last_ping = time.time()
    try:
        await response.write(_sse(room.snapshot(viewer_id)))
        while not room.closed:
            try:
                payload = await asyncio.wait_for(queue.get(), tick)
            except asyncio.TimeoutError:
                if gone(request):
                    break             # 창을 닫았다 — finally가 방에서 빼준다
                if time.time() - last_ping < heartbeat:
                    continue
                last_ping = time.time()
                # 토큰이 만료되지 않도록 같이 연장한다 (한 판이 길어져도 링크가 안 죽는다)
                entry["expires"] = time.time() + config["lobby_config"]["token_ttl_seconds"]
                await response.write(b": ping\n\n")
                continue
            if payload is _KICK:
                # 같은 사람이 다른 창에서 접속했다 — 왜 끊겼는지 알려주고 닫는다
                await response.write(_sse({"type": "room", "kicked": "elsewhere"}))
                break
            # 대결이 시작되면 매치 쪽 알림도 받아야 한다 (시작 시점에 구독을 옮겨 붙인다)
            if room.match is not None and queue not in room.match.subscribers:
                room.match.subscribers.add(queue)
            if viewer_id not in room.members:
                # 추방당했거나 스스로 나갔다 — **왜 빠졌는지 알려주고** 화면을 로비로 보낸다
                await response.write(_sse({
                    "type": "room",
                    "removed": room.removed.pop(viewer_id, "web_room_left_notice"),
                }))
                break
            await response.write(_sse(room.snapshot(viewer_id)))
        if room.closed:
            await response.write(_sse(room.snapshot(viewer_id)))
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        room.subscribers.discard(queue)
        if room.match is not None:
            room.match.subscribers.discard(queue)
        if (live_streams.get(viewer_id) or (None, None, None))[1] is queue:
            live_streams.pop(viewer_id, None)
        room.viewers[viewer_id] = max(0, room.viewers.get(viewer_id, 1) - 1)
        if not room.viewers[viewer_id]:
            room.viewers.pop(viewer_id, None)
            if viewer_id in room.members and not room.closed:
                asyncio.create_task(drop_disconnected(room, viewer_id))
    return response


async def handle_lobby_stream(request):
    """로비(방 목록 + 접속자) 실시간 스트림.

    **아무 조작도 없이 오래 붙잡고 있으면 끊는다**(`lobby_config.idle_timeout_seconds`, 10분).
    기준은 '접속한 지'가 아니라 **'마지막으로 뭔가 한 지'**다 — 목록을 새로고침하거나 방을
    만들거나 입장을 시도하면 시간이 다시 시작된다. 창만 띄워두고 잊은 접속이 쌓이면 자리와
    메모리를 그대로 먹기 때문이다.

    **같은 사람의 창은 하나만 유지한다** — 새 창이 붙으면 옛 창을 끊는다.
    """
    entry = resolve_lobby_token(request)
    if entry is None:
        return web.json_response({"error": "expired"}, status=403)

    conf = config["lobby_config"]
    viewer_id = entry["user_id"]
    # 접속 상한은 **사람 수**로 센다 (탭을 여러 개 열어도 한 명). 이미 들어와 있으면 통과.
    if viewer_id not in online_user_ids() and len(online_user_ids()) >= conf["max_online"]:
        return web.json_response({"error": "lobby_full"}, status=429)

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    await response.prepare(request)

    cid = client_id(request)
    close_other_streams(viewer_id, cid, "lobby")
    session = LobbySession(viewer_id, entry["name"], entry["lang"])
    lobby_sessions.add(session)
    live_streams[viewer_id] = (cid, session.queue, request)

    heartbeat = config["pvp_config"].get("stream_heartbeat_seconds", 15)
    idle_limit = conf["idle_timeout_seconds"]
    tick = min(2, heartbeat)
    last_ping = time.time()
    try:
        # 목록은 화면이 /api/lobby로 직접 받아간다. 여기로는 **초대와 정리 안내만** 나간다.
        await response.write(b": ready\n\n")
        while True:
            try:
                payload = await asyncio.wait_for(session.queue.get(), tick)
            except asyncio.TimeoutError:
                if gone(request):
                    break             # 창을 닫았다 — 접속자 집계에서 바로 빠진다
                if time.time() - session.last_action > idle_limit:
                    await response.write(_sse({"type": "lobby", "kicked": "idle"}))
                    break
                if time.time() - last_ping < heartbeat:
                    continue
                last_ping = time.time()
                entry["expires"] = time.time() + conf["token_ttl_seconds"]
                await response.write(b": ping\n\n")
                continue
            if payload is _KICK:
                await response.write(_sse({"type": "lobby", "kicked": "elsewhere"}))
                break
            # 전파기가 이미 만들어 보낸 JSON을 그대로 흘려보낸다 (여기서 다시 만들지 않는다)
            await response.write(f"data: {payload}\n\n".encode("utf-8"))
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        lobby_sessions.discard(session)
        if (live_streams.get(viewer_id) or (None, None, None))[1] is session.queue:
            live_streams.pop(viewer_id, None)
    return response


def gone(request) -> bool:
    """클라이언트가 이미 끊었는지.

    **aiohttp는 상대가 끊어도 핸들러를 깨워주지 않는다** — 다음 쓰기를 시도할 때에야 안다.
    그래서 하트비트(15초)만 믿으면 창을 닫고 15초가 지나서야 방에서 빠졌다(실제로 그랬다).
    대기를 짧게 끊고 매번 이걸 확인하면 몇 초 안에 알아챈다. 확인 자체는 공짜라 트래픽이
    늘지 않는다 — 실제 `: ping`은 여전히 하트비트 주기로만 보낸다.
    """
    transport = request.transport
    return transport is None or transport.is_closing()


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def handle_match_deck(request):
    """웹에서 이번 대결에 쓸 덱을 고른다. 양쪽이 다 고르면 1라운드가 시작된다."""
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    match, viewer_id = room.match, entry["user_id"]
    if match is None or viewer_id not in match.sides:
        return web.json_response({"error": "not_a_player"}, status=403)

    try:
        number = int((await request.json()).get("deck_number"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    side = match.sides[viewer_id]
    async with match.lock:
        if match.finished or side.deck_number is not None:
            return web.json_response({"error": "already_chosen"}, status=409)
        if number not in side.decks:
            return web.json_response({"error": "no_such_deck"}, status=400)
        side.choose_deck(number)
        # **"둘 다 골랐다"는 판단을 값을 넣는 이 잠금 안에서 해야 한다.** 밖에서 다시 보면
        # 두 사람이 동시에 고른 경우 양쪽 요청이 모두 "상대도 골랐다"를 보고 1라운드를
        # 두 번 시작해버린다 (라운드 번호가 2로 건너뛰고 타이머도 두 개가 된다).
        both_chosen = match.other(viewer_id).deck_number is not None and not match.finished

    await match.broadcast()

    if both_chosen:
        async with match.lock:
            if not match.finished and match.round_no == 0:
                await run_match_step(match, start_round(match))
    return web.json_response({"ok": True})


async def handle_match_pick(request):
    """웹에서 카드를 낸다. 디스코드 선택 메뉴와 **같은 경로를 타야** 경합이 안 난다."""
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    match, viewer_id = room.match, entry["user_id"]
    if match is None or viewer_id not in match.sides:
        return web.json_response({"error": "not_a_player"}, status=403)

    try:
        hero_id = int((await request.json()).get("hero_id"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    side = match.sides[viewer_id]
    async with match.lock:
        if match.finished or side.pick is not None:
            # 이미 냈거나(디스코드에서 먼저 골랐거나 타임아웃 자동선택) 매치가 끝난 상태
            return web.json_response({"error": "already_picked"}, status=409)
        card = next((c for c in side.remaining if c["hero_id"] == hero_id), None)
        if card is None:
            return web.json_response({"error": "not_in_hand"}, status=400)
        side.pick = card
        # 덱 선택과 같은 이유로 "둘 다 냈다"도 이 잠금 안에서 판단한다. 그리고 판정은
        # **낸 그 라운드에 대해서만** 해야 한다 — 기다리는 동안 타임아웃이 먼저 판정하고
        # 다음 라운드로 넘어갔을 수 있다.
        both_picked = match.other(viewer_id).pick is not None and not match.finished
        picked_round = match.round_no

    await match.broadcast()

    if both_picked:
        async with match.lock:
            if (not match.finished and match.round_no == picked_round
                    and match.challenger.pick and match.opponent.pick):
                await run_match_step(match, resolve_round(match))
    return web.json_response({"ok": True})


async def _body(request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


async def handle_lobby_labels(request):
    """로비 화면이 처음 한 번만 받아가는 문구·설정."""
    entry = resolve_lobby_token(request)
    if entry is None:
        return web.json_response({"error": "expired"}, status=403)
    lang = entry["lang"]
    conf = config["lobby_config"]
    return web.json_response({
        "me": {"id": str(entry["user_id"]), "name": entry["name"]},
        "min_wager": config["pvp_config"]["min_wager"],
        "password_max_length": conf["password_max_length"],
        "idle_minutes": max(1, conf["idle_timeout_seconds"] // 60),
        "refresh_cooldown": conf.get("refresh_cooldown_seconds", 5),
        # 방 만들기 창의 배경 드롭박스 항목. 이름은 보는 사람 언어로 골라서 내려보낸다.
        "backgrounds": [{"id": item["id"],
                         "name": item["name"].get(lang) or item["name"].get("en-US", item["id"])}
                        for item in config["background_config"]["list"]],
        "messages": {
            key: get_msg(lang, key)
            for key in (
                "web_lobby_title", "web_lobby_online", "web_lobby_users",
                "web_lobby_users_empty", "web_lobby_in_rooms", "web_lobby_rooms",
                "web_lobby_rooms_empty", "web_lobby_create", "web_lobby_create_title",
                "web_lobby_wager_label", "web_lobby_private_label", "web_lobby_password_label",
                "web_lobby_confirm", "web_lobby_cancel", "web_lobby_join",
                "web_lobby_password_prompt", "web_lobby_password_wrong",
                "web_lobby_password_needed", "web_lobby_banned", "web_lobby_room_gone",
                "web_lobby_room_full", "web_lobby_rooms_full", "web_lobby_full",
                "web_lobby_idle_kicked", "web_lobby_expired", "web_lobby_wager_invalid",
                "web_lobby_poor", "web_lobby_already_in_room", "web_lobby_invited",
                "web_lobby_invite_accept", "web_lobby_invite_decline", "web_lobby_my_points",
                "web_lobby_refresh", "web_lobby_refresh_wait", "web_lobby_refreshed",
                "web_lobby_kicked_elsewhere",
                "web_lobby_name_label", "web_lobby_name_placeholder", "web_lobby_default_name",
                "web_lobby_background_label", "web_lobby_background_random",
                "web_lobby_filter_visibility", "web_lobby_filter_all", "web_lobby_filter_public",
                "web_lobby_filter_private", "web_lobby_filter_wager", "web_lobby_filter_search",
                "web_lobby_filter_search_hint", "web_lobby_filter_none",
                "web_room_host", "web_room_wager", "web_room_friendly", "web_room_private",
                "web_room_people", "web_room_playing", "web_match_lost_connection",
            )
        },
    })


async def handle_room_create(request):
    """방을 만든다. 만든 사람이 방장이 되고 상대 자리는 비어 있다."""
    entry = resolve_lobby_token(request)
    if entry is None:
        return web.json_response({"error": "expired"}, status=403)
    user_id, lang = entry["user_id"], entry["lang"]
    touch_lobby(user_id)
    if room_of(user_id) is not None:
        return web.json_response({"error": "already_in_room"}, status=409)
    if len(rooms) >= config["lobby_config"]["max_rooms"]:
        return web.json_response({"error": "rooms_full"}, status=429)

    body = await _body(request)
    try:
        wager = int(body.get("wager") or 0)
    except (TypeError, ValueError):
        return web.json_response({"error": "bad_wager"}, status=400)
    if wager < 0 or (wager and wager < config["pvp_config"]["min_wager"]):
        return web.json_response({"error": "bad_wager"}, status=400)

    # 배경은 **서버가 다시 확인한다** — 화면의 드롭박스는 편의일 뿐이고, 주소를 직접
    # 두드리면 아무 문자열이나 보낼 수 있다. 등록되지 않은 id면 거절한다.
    background = str(body.get("background") or BACKGROUND_RANDOM)
    if background != BACKGROUND_RANDOM and background not in background_ids():
        return web.json_response({"error": "bad_background"}, status=400)

    password = str(body.get("password") or "")[:config["lobby_config"]["password_max_length"]]
    name = str(body.get("name") or "").strip()[:40] or get_msg(lang, "web_lobby_default_name",
                                                               name=entry["name"])

    # 판돈을 못 내면 방부터 못 만들게 한다 — 만들어두고 시작만 계속 실패하면 자리만 막는다
    if wager and user_id not in await affordable([user_id], wager):
        return web.json_response({"error": "poor"}, status=409)

    global _room_seq
    _room_seq += 1
    room = Room(_room_seq, Player(user_id, entry["name"]), name, wager, password, lang, background)
    rooms[room.room_id] = room
    return web.json_response({"room_id": room.room_id})


async def handle_room_join(request):
    """방에 들어간다. 상대 자리가 비어 있고 판돈을 낼 수 있으면 상대, 아니면 관전자."""
    entry = resolve_lobby_token(request)
    if entry is None:
        return web.json_response({"error": "expired"}, status=403)
    user_id = entry["user_id"]
    touch_lobby(user_id)

    body = await _body(request)
    try:
        room_id = int(body.get("room_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad_request"}, status=400)
    room = rooms.get(room_id)
    if room is None or room.closed:
        return web.json_response({"error": "room_gone"}, status=404)
    if user_id in room.members:
        return web.json_response({"room_id": room.room_id})          # 이미 들어가 있다
    if room_of(user_id) is not None:
        return web.json_response({"error": "already_in_room"}, status=409)
    if user_id in room.banned:
        return web.json_response({"error": "banned"}, status=403)

    # **초대받은 사람은 비밀번호를 묻지 않는다** — 방 안에 있는 사람이 직접 부른 것이라
    # 이미 확인을 거친 셈이다. 목록을 보고 직접 들어오는 경우에는 관전이라도 물어본다.
    if room.password and not room.is_invited(user_id):
        if str(body.get("password") or "") != room.password:
            return web.json_response({"error": "wrong_password"}, status=403)

    async with room.lock:
        if room.closed:
            return web.json_response({"error": "room_gone"}, status=404)
        limit = config["pvp_config"].get("max_spectators", 50)
        if len(room.members) >= limit + 2:
            return web.json_response({"error": "room_full"}, status=429)
        room.members[user_id] = Player(user_id, entry["name"])
        room.invited.pop(user_id, None)
        if None in room.seats and not room.playing:
            # 판돈을 못 내면 자리에 안 앉힌다 (승격 규칙과 같은 기준).
            # 들여보내되 **왜 관전자가 됐는지는 알려준다** — 안 그러면 자리가 비어 있는데
            # 왜 나만 못 앉는지 알 길이 없다.
            if user_id in await affordable([user_id], room.wager):
                room.seats[room.seats.index(None)] = user_id
            else:
                room.notices[user_id] = {"key": "web_room_joined_poor",
                                         "vars": {"wager": room.wager}}
    await room.broadcast()
    return web.json_response({"room_id": room.room_id})


async def handle_room_ready(request):
    """상대가 준비를 켜고 끈다. 준비돼야 방장이 시작할 수 있다."""
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    user_id = entry["user_id"]
    # 방장은 준비를 누르지 않는다 — 시작 버튼을 누르는 쪽이라 준비한 것으로 본다.
    if room.seat_of(user_id) is None or user_id == room.host_id or room.match is not None:
        return web.json_response({"error": "not_opponent"}, status=403)
    if (await _body(request)).get("ready"):
        room.ready.add(user_id)
    else:
        room.ready.discard(user_id)
    await room.broadcast()
    return web.json_response({"ok": True})


async def handle_room_start(request):
    """방장이 대결을 시작한다. **판돈은 바로 여기서 양쪽에서 빠진다.**"""
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    if entry["user_id"] != room.host_id:
        return web.json_response({"error": "not_host"}, status=403)

    async with room.lock:
        if not room.can_start:
            return web.json_response({"error": "not_ready"}, status=409)
        host = room.members[room.seats[0]]        # 자리 순서가 그대로 도전자/상대가 된다
        opponent = room.members[room.seats[1]]

        async with bot.pool.acquire() as conn:
            decks = {}
            for player in (host, opponent):
                decks[player.id] = await fetch_battle_decks(conn, player.id, room.lang)
                if not decks[player.id]:
                    return web.json_response({"error": "no_deck", "who": str(player.id)}, status=409)
            match_id, reason = await create_match(
                conn, host.id, opponent.id, room.wager,
                challenger_name=host.display_name, opponent_name=opponent.display_name)
            if reason:
                return web.json_response({"error": reason}, status=409)
            # 판돈을 실제로 묶는다. 여기서 실패하면 매치 행도 같이 취소해야 한다.
            reason = await accept_match(conn, match_id)
            if reason:
                await cancel_match(conn, match_id)
                return web.json_response({"error": reason}, status=409)

        match = PvpMatch(match_id, PvpSide(host, decks[host.id]),
                         PvpSide(opponent, decks[opponent.id]), room.wager, room.lang)
        match.room = room
        room.match = match
        # 랜덤 방은 **판마다 무대를 다시 뽑는다.** 고정 배경을 고른 방은 그대로다.
        room.current_background = pick_background(room.background)
        room.last_active = time.time()      # 방 폭파 시계를 되돌린다
        live_matches[match_id] = match
        # 이미 붙어 있는 방 구독자들이 매치 알림도 받도록 그대로 넘겨준다
        match.subscribers |= room.subscribers
        match.arm(config["pvp_config"]["pick_timeout_seconds"], lambda: deck_timed_out(match))

    await room.broadcast()
    return web.json_response({"ok": True, "match_id": match_id})


async def handle_room_rematch(request):
    """끝난 판의 결과 화면을 걷고 방을 대기실로 되돌린다 ('다시 매칭').

    **시간이 지나면 저절로 돌아가게 하지 않는다** — 결과를 얼마나 들여다볼지는 사람마다
    다르고, 자동으로 걷으면 읽던 중에 사라진다. 대신 **방장만** 누를 수 있게 한다:
    방 전체의 화면을 되돌리는 조작이라, 아직 결과를 보고 있는 사람의 화면까지 같이 치운다.
    다음 판을 시작하는 것도 방장이므로 권한을 한 사람에게 모아두는 편이 헷갈리지 않는다.
    """
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    if entry["user_id"] != room.host_id:
        return web.json_response({"error": "not_host"}, status=403)

    async with room.lock:
        if room.match is None:
            return web.json_response({"ok": True})
        if not room.match.finished:
            return web.json_response({"error": "in_match"}, status=409)
        room.match = None
        # 진 쪽이 판돈을 더는 못 낼 수 있다 — 그대로 두면 시작이 계속 실패하므로 자리를 바꾼다
        await recheck_seats(room)
    await room.broadcast()
    return web.json_response({"ok": True})


async def handle_room_leave(request):
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    async with room.lock:
        await leave_room(room, entry["user_id"])
    return web.json_response({"ok": True})


async def handle_room_kick(request):
    """방장이 내보낸다. **대결이 시작된 뒤에는 관전자만** 내보낼 수 있다."""
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    if entry["user_id"] != room.host_id:
        return web.json_response({"error": "not_host"}, status=403)
    try:
        target = int((await _body(request)).get("user_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad_request"}, status=400)
    if target == room.host_id or target not in room.members:
        return web.json_response({"error": "bad_target"}, status=400)
    if room.playing and target in room.seats:
        # 대결 중에 상대를 내보낼 수 있으면 지고 있을 때 판을 엎을 수 있다
        return web.json_response({"error": "in_match"}, status=409)

    async with room.lock:
        room.banned.add(target)
        # 쫓겨난 사람은 **방 화면에 그대로 두면 안 된다** — 스트림만 끊기고 화면은 방에 남아
        # 있어서, 본인은 아직 방에 있는 줄 안다. 사유를 남겨두면 스트림이 그걸 실어 보내고
        # 화면이 안내를 띄운 뒤 로비로 돌려보낸다.
        room.removed[target] = "web_room_kicked"
        await leave_room(room, target)
    return web.json_response({"ok": True})


async def handle_room_seat(request):
    """빈 자리에 앉거나(sit), 관전으로 물러난다(stand).

    **대결 중에는 자리를 바꿀 수 없다** — 카드를 내다 말고 빠지면 판이 성립하지 않는다.
    물러나면 빈 자리는 곧바로 관전자 중에서 채워지므로(`fill_seats`), 관전자가 있으면
    사실상 교대가 된다. 방장이 물러나도 **방장 권한은 그대로 남는다** (자리와 권한은 별개).
    """
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    user_id = entry["user_id"]
    if room.playing:
        return web.json_response({"error": "in_match"}, status=409)

    body = await _body(request)
    async with room.lock:
        if body.get("sit"):
            if room.seat_of(user_id) is not None:
                return web.json_response({"ok": True})
            if None not in room.seats:
                return web.json_response({"error": "seat_taken"}, status=409)
            if user_id not in await affordable([user_id], room.wager):
                return web.json_response({"error": "poor", "wager": room.wager}, status=409)
            room.seats[room.seats.index(None)] = user_id
            room.standing.discard(user_id)
        else:
            seat = room.seat_of(user_id)
            if seat is None:
                return web.json_response({"ok": True})
            room.seats[seat] = None
            room.ready.discard(user_id)
            room.standing.add(user_id)
            room.notices[user_id] = {"key": "web_room_stood", "vars": {}}
            await fill_seats(room)
    await room.broadcast()
    return web.json_response({"ok": True})


async def handle_room_invite(request):
    """방 안에 있는 사람(관전자 포함)이 로비에 있는 사람을 부른다."""
    resolved = resolve_room_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    room, entry = resolved
    try:
        target = int((await _body(request)).get("user_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad_request"}, status=400)
    if target in room.members or target in room.banned:
        return web.json_response({"error": "bad_target"}, status=400)
    if not any(session.user_id == target for session in lobby_sessions):
        return web.json_response({"error": "not_in_lobby"}, status=404)

    room.invited[target] = time.time() + config["lobby_config"]["invite_ttl_seconds"]
    await notify_invite(target, room)
    return web.json_response({"ok": True})


async def start_web_server():
    """Render는 웹 서비스가 PORT를 열고 있어야 해서, 헬스체크 겸 덱 편성 페이지를 여기서 서빙한다.

    Render 무료 플랜은 들어오는 요청이 한동안 없으면 인스턴스를 재우고(스핀다운), 그동안엔
    봇도 같이 멈춘다. 그래서 UptimeRobot 같은 외부 모니터가 여기를 주기적으로 찔러줘야 한다.
    """
    start_janitor()
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="Arcade bot is online!"))
    app.router.add_get("/health", handle_health)
    app.router.add_get("/deck", handle_deck_page)
    app.router.add_get("/api/deck", handle_deck_data)
    app.router.add_get("/api/history", handle_match_history)
    app.router.add_get("/api/summons", handle_summon_history)
    app.router.add_get("/lobby", handle_lobby_page)
    app.router.add_get("/api/lobby", handle_lobby_state)
    app.router.add_get("/api/lobby/labels", handle_lobby_labels)
    app.router.add_get("/api/lobby/stream", handle_lobby_stream)
    app.router.add_post("/api/lobby/create", handle_room_create)
    app.router.add_post("/api/lobby/join", handle_room_join)
    app.router.add_get("/room", handle_room_page)
    app.router.add_post("/api/room/ready", handle_room_ready)
    app.router.add_post("/api/room/start", handle_room_start)
    app.router.add_post("/api/room/rematch", handle_room_rematch)
    app.router.add_post("/api/room/leave", handle_room_leave)
    app.router.add_post("/api/room/kick", handle_room_kick)
    app.router.add_post("/api/room/seat", handle_room_seat)
    app.router.add_post("/api/room/invite", handle_room_invite)
    app.router.add_get("/api/room/invitable", handle_room_invitable)
    app.router.add_get("/match", handle_room_page)
    app.router.add_get("/api/match", handle_room_state)
    app.router.add_get("/api/match/labels", handle_match_labels)
    app.router.add_get("/api/match/stream", handle_room_stream)
    app.router.add_post("/api/match/deck", handle_match_deck)
    app.router.add_post("/api/match/pick", handle_match_pick)
    app.router.add_post("/api/deck", handle_deck_save)
    app.router.add_post("/api/deck/reset-enhance", handle_deck_reset_enhance)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"🌐 웹서버 구동 완료 (포트: {port})")
    return runner  # 테스트가 끝나고 포트를 닫을 수 있도록 돌려준다

def web_base_url() -> str:
    """덱 링크에 쓸 공개 URL. Render가 자동으로 넣어주는 값을 우선 쓰고, 없으면 설정/로컬 순."""
    return (config["web_config"]["base_url"]
            or os.getenv("RENDER_EXTERNAL_URL")
            or f"http://localhost:{os.environ.get('PORT', 10000)}").rstrip("/")

async def do_arcade(interaction: discord.Interaction):
    """카드게임 로비 링크 발급. 패널 버튼에서 호출한다.

    **로비는 웹에만 있고 디스코드 버튼은 입장권만 준다.** 링크 버튼은 눌러도 봇에 신호가
    오지 않아서(= 누가 눌렀는지 알 수 없다) 한 번에 열어줄 수 없다 — 그래서 일반 버튼으로
    받아 본인 확인을 하고, 그 사람 이름이 박힌 토큰 링크를 나만 보이는 메시지로 준다.
    닉네임을 여기서 받아두는 이유는 members 인텐트가 없어 나중에 id로 되찾을 수 없기 때문이다.
    """
    await interaction.response.defer(ephemeral=True)
    lang = resolve_lang(interaction)
    deck_size = config["deck_config"]["deck_size"]

    try:
        async with bot.pool.acquire() as conn:
            owned = await conn.fetchval(
                "SELECT count(*) FROM user_cards WHERE user_id = $1", interaction.user.id)
    except Exception as e:
        print(f"⚠️ 로비 입장 처리 중 DB 오류: {e}")
        await interaction.followup.send(get_msg(lang, "arcade_error"), ephemeral=True)
        return

    # 덱이 없으면 방에 들어가도 시작할 수 없으니 **로비 입장 자체를 막는다**.
    # 그냥 막기만 하면 뭘 해야 하는지 모르므로 덱 편성 링크를 같이 준다 — 덱을 짜고
    # 다시 카드게임을 누르면 된다.
    if owned < deck_size:
        deck_token = issue_deck_token(interaction.user.id, lang)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link,
            label=get_msg(lang, "deck_link_button"),
            url=f"{web_base_url()}/deck?token={deck_token}",
        ))
        await interaction.followup.send(
            get_msg(lang, "arcade_no_deck", size=deck_size), view=view, ephemeral=True)
        return

    token = issue_lobby_token(interaction.user.id, interaction.user.display_name, lang)
    embed = discord.Embed(
        title=get_msg(lang, "arcade_link_title"),
        description=get_msg(lang, "arcade_link_desc"),
        color=discord.Color.blurple(),
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.link,
        label=get_msg(lang, "arcade_link_button"),
        url=f"{web_base_url()}/lobby?token={token}",
    ))
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def do_deck(interaction: discord.Interaction):
    """덱 편성 웹페이지 링크 발급. 패널 버튼에서 호출한다."""
    await interaction.response.defer(ephemeral=True)
    lang = resolve_lang(interaction)

    try:
        async with bot.pool.acquire() as conn:
            owned = await conn.fetchval("SELECT count(*) FROM user_cards WHERE user_id = $1", interaction.user.id)
    except Exception as e:
        print(f"⚠️ 덱 설정 처리 중 DB 오류: {e}")
        await interaction.followup.send(get_msg(lang, "deck_error"), ephemeral=True)
        return

    deck_size = config["deck_config"]["deck_size"]
    if owned < deck_size:
        await interaction.followup.send(
            get_msg(lang, "deck_not_enough_cards", need=deck_size, have=owned), ephemeral=True
        )
        return

    token = issue_deck_token(interaction.user.id, lang)
    minutes = config["web_config"]["token_ttl_seconds"] // 60

    embed = discord.Embed(
        title=get_msg(lang, "deck_link_title"),
        description=get_msg(lang, "deck_link_desc", minutes=minutes),
        color=discord.Color.blurple(),
    )
    # 소환 결과와 같은 방식으로 링크 버튼을 단다 — 주소를 본문에 그대로 적으면 토큰이 붙은 긴
    # URL이 노출된다. 링크 전용 View는 custom_id가 없어 discord.py의 view store에 남지 않으므로
    # timeout=None을 줘도 메모리에 쌓이지 않는다 (2.7.1 소스로 확인).
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.link,
        label=get_msg(lang, "deck_link_button"),
        url=f"{web_base_url()}/deck?token={token}",
    ))
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# --- PVP 진행 ---------------------------------------------------------------
# 매치 진행 상태는 메모리에만 둔다. 봇이 재시작되면 버튼(View)이 어차피 죽어서 이어갈 수 없고,
# 묶여 있던 판돈은 시작할 때 refund_stale_matches()가 되돌려준다.

class PvpSide:
    """한 참가자 쪽 상태 (덱, 남은 카드, 이번 라운드에 낸 카드)."""

    def __init__(self, user, decks: dict[int, list[dict]]):
        self.user = user            # Player (id + display_name). 디스코드 객체가 아니다.
        self.decks = decks
        self.deck_number: int | None = None
        self.remaining: list[dict] = []
        self.pick: dict | None = None
        self.wins = 0

    def choose_deck(self, number: int) -> None:
        self.deck_number = number
        self.remaining = list(self.decks[number])

class PvpMatch:
    def __init__(self, match_id: int, challenger: PvpSide, opponent: PvpSide, wager: int, lang: str):
        self.match_id = match_id
        self.sides = {challenger.user.id: challenger, opponent.user.id: opponent}
        self.challenger, self.opponent = challenger, opponent
        self.wager = wager
        self.lang = lang
        self.round_no = 0
        self.finished = False
        self.lock = asyncio.Lock()  # 두 사람이 동시에 눌러도 라운드가 두 번 진행되지 않도록
        # 이 매치가 벌어지고 있는 방. 끝났을 때 방 화면을 다시 대기실로 돌리는 데 쓴다.
        self.room = None
        # 웹 관전/조작 화면에 상태를 밀어줄 SSE 구독자들. 참가자와 관전자가 같이 들어 있다.
        # 실측으로 연결 1개가 156KB라 수천 개까지 버티지만, 관전자는 상한을 둔다.
        self.subscribers: set[asyncio.Queue] = set()
        self.spectators = 0          # 참가자를 뺀 순수 관전자 수 (화면에 "관전자 N명"으로 표시)
        self.last_round: dict | None = None   # 직전 라운드 판정 결과 + 근거 (이펙트용)
        self.winner_id: int | None = None
        self.end_reason: str | None = None
        # 제한 시간은 서버가 잰다. 예전에는 discord.ui.View의 timeout이 재줬는데, 조작이
        # 웹으로 옮겨가면서 View가 사라져 직접 타이머를 돌려야 한다.
        # deadline은 유닉스 시각이라 화면이 남은 초를 직접 계산해 보여줄 수 있다.
        self.deadline: float | None = None
        self._timer: asyncio.Task | None = None
        self.web_sides: set[int] = set()      # 웹 화면을 열어둔 참가자 id

    def arm(self, seconds: int, on_expire) -> None:
        """`seconds` 뒤에 `on_expire()`를 돌리는 타이머를 건다 (이전 타이머는 취소)."""
        self.disarm()
        self.deadline = time.time() + seconds

        async def run():
            try:
                await asyncio.sleep(seconds)
            except asyncio.CancelledError:
                return
            await run_match_step(self, on_expire())

        self._timer = asyncio.create_task(run())

    def disarm(self) -> None:
        """건 타이머를 해제한다.

        **만료 콜백 안에서 불릴 때 자기 자신을 취소하면 안 된다.** 타임아웃이 터지면
        `round_timed_out`/`deck_timed_out` -> `resolve_round`/`finish_match` 순으로 이어지는데,
        그 안에서 `disarm()`이 지금 돌고 있는 바로 그 타이머 태스크를 취소해버려서 **다음 await에서
        CancelledError가 터지고 진행이 통째로 중단**됐다. `CancelledError`는 `Exception`이 아니라
        `BaseException`이라 `run_match_step()`의 except에도 안 걸려 아무 안내 없이 조용히 멈췄다.
        증상은 두 가지로 나타났다 — 덱을 아무도 안 고르면 판돈이 'playing'으로 묶인 채 매치가
        영원히 안 끝나고, 라운드에서 안 내면 첫 판정 직후 화면이 멈췄다.
        (테스트는 `round_timed_out()`을 직접 불러서 검증했기 때문에 이 경로를 못 잡았다.)
        """
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self.deadline = None

    def snapshot(self, viewer_id: int | None) -> dict:
        """웹 화면이 그릴 수 있는 형태로 현재 상태를 통째로 담아준다.

        증분(diff)이 아니라 매번 전체 상태를 보낸다 — 한 판의 상태가 몇 KB밖에 안 되고(실측
        이벤트 전파 CPU는 400연결에서도 측정 하한 미만), 중간에 이벤트를 놓친 클라이언트나
        새로 들어온 관전자가 알아서 맞춰지므로 재동기화 로직이 아예 필요 없다.

        `viewer_id`가 참가자면 그 사람이 '아래쪽', 상대가 '위쪽'이 된다. 관전자(None)는
        도전자를 아래쪽에 둔다.
        """
        me = self.sides.get(viewer_id) or self.challenger
        them = self.other(me.user.id)

        def side_state(side: PvpSide, reveal: bool, own: bool) -> dict:
            # 덱을 아직 안 골랐으면 카드가 없다. 고른 뒤에는 양쪽 덱이 규칙상 전부 공개된다.
            cards = [web_card(c) for c in side.remaining] if reveal else []
            return {
                "name": side.user.display_name,
                "wins": side.wins,
                "deck_chosen": side.deck_number is not None,
                "remaining": cards,
                "remaining_count": len(side.remaining),
                # **낸 카드는 본인 것만 내려보낸다.** 예전에는 상대 것도 같이 실려 나갔다
                # (`side.pick and self.last_round` 조건이라 2라운드부터 계속 새어나갔다).
                # 화면이 그 값을 쓰지도 않았지만, 스냅샷은 개발자도구로 그냥 보이므로
                # **상대가 낸 카드를 보고 내 카드를 고를 수 있었다** — 판돈이 걸린 게임에서
                # 치명적이다. 판정이 끝난 카드는 `last_round`로 따로 나가므로 연출에는 지장이 없다.
                "picked": side.pick is not None,
                "pick": web_card(side.pick) if (own and side.pick) else None,
            }

        both_chose = self.challenger.deck_number is not None and self.opponent.deck_number is not None
        return {
            "match_id": self.match_id,
            "round": self.round_no,
            "wager": self.wager,
            "finished": self.finished,
            "phase": ("finished" if self.finished
                      else "deck" if not both_chose
                      else "round"),
            "me": side_state(me, both_chose, own=viewer_id in self.sides),
            "them": side_state(them, both_chose, own=False),
            "is_player": viewer_id in self.sides,
            # last_round는 도전자/상대 축으로 담기므로, 화면이 '나/상대' 축으로 바꿀 수 있게 알려준다
            "is_challenger": me.user.id == self.challenger.user.id,
            "my_turn": viewer_id in self.sides and self.sides[viewer_id].pick is None and both_chose,
            "spectators": self.spectators,
            "last_round": self.last_round,
            "deadline": self.deadline,          # 남은 초는 화면이 이 시각으로 계산한다
            # 덱을 아직 안 고른 참가자에게만 고를 거리를 내려준다 (관전자에겐 안 보낸다)
            "my_decks": (
                {str(number): [web_card(c) for c in cards] for number, cards in sorted(me.decks.items())}
                if viewer_id in self.sides and me.deck_number is None else None
            ),
            "winner": ("me" if self.winner_id == me.user.id
                       else "them" if self.winner_id == them.user.id
                       else "draw" if self.finished and not self.end_reason else None),
            "end_reason": self.end_reason,
            # 판돈은 수락 시점에 이미 차감(에스크로)돼 있으므로, 최종 증감은
            # 승리 +판돈 / 패배 -판돈 / 무효·무승부 0 이다.
            "points_delta": (
                0 if not self.finished or not self.wager or self.end_reason or self.winner_id is None
                else self.wager if self.winner_id == me.user.id else -self.wager
            ),
            "deck_size": config["deck_config"]["deck_size"],
        }

    async def broadcast(self) -> None:
        """모든 구독자에게 현재 상태를 밀어준다.

        구독자마다 보는 관점(누가 아래쪽인지)이 달라서 큐에는 viewer_id를 같이 담아두고
        각자 자기 관점의 스냅샷을 만들어 보낸다.
        """
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)      # None = "상태가 바뀌었으니 새로 만들어 보내라"
            except asyncio.QueueFull:
                pass

    def other(self, user_id: int) -> PvpSide:
        return self.opponent if user_id == self.challenger.user.id else self.challenger

live_matches: dict[int, PvpMatch] = {}   # match_id -> 진행 중인 매치
# 웹에서 조작할 권한은 **로비 토큰 + 방 소속**으로 판단한다 (resolve_room_view 참고).
# 예전에는 매치마다 토큰을 따로 발급했는데, 방이 생기면서 그 역할을 방 소속이 대신한다.


def web_card(card: dict) -> dict:
    """전투 카드 dict를 웹 화면이 쓰는 형태로. 이미지 파일명은 한글 원본 그대로 넘긴다."""
    return {
        "hero_id": card["hero_id"],
        "name": card["display_name"],
        "image_name": card["name"],
        "grade": card["grade"],
        "job": card["job"],
        "element": card["element"],
        "stats": {key: card[key] for key in STAT_KEYS},
    }

def timer_line(lang: str, seconds: int) -> str:
    """남은 제한 시간을 디스코드 상대 타임스탬프(`<t:...:R>`)로 표시하는 한 줄.

    봇이 매초 메시지를 고쳐 쓰는 대신 디스코드 클라이언트가 알아서 카운트다운해주므로
    API 호출이나 편집 레이트리밋 부담이 전혀 없다 (대신 초 단위가 딱 맞지는 않는다).
    View의 타임아웃은 메시지가 전송된 시점부터 재므로, 이 줄은 메시지를 보내기 직전에 만들어야 한다.
    """
    return get_msg(lang, "match_time_left", time=f"<t:{int(time.time()) + seconds}:R>")

async def run_match_step(match: PvpMatch, step) -> None:
    """매치 진행 한 단계를 돌리되, 예상 못 한 예외가 나면 판돈을 돌려주고 매치를 끝낸다.

    View 콜백에서 예외가 그냥 밖으로 새면 discord.py가 로그만 찍고 끝이라 **매치가 메모리에
    살아 있는 채로 멈춘다.** 두 사람은 아무 안내도 못 받고 화면이 멈춘 것처럼 보이고, 더 나쁜 건
    DB의 판돈이 'playing'으로 묶인 채 남아서 **봇을 재시작해야(refund_stale_matches) 포인트가
    풀린다**는 점이다. 실제로 에셋 버킷이 403을 주면서 이 상황이 났다 (카드 이미지를 못 그리면
    라운드 판정도 계속 실패하므로, 이미지 없이 진행하기보다 판돈을 돌려주고 끊는 게 맞다).
    """
    try:
        await step
    except Exception:
        traceback.print_exc()
        await finish_match(match, None, reason_key="match_cancelled_error")

async def finish_match(match: PvpMatch, winner_id: int | None, reason_key: str | None = None) -> None:
    """정산하고 양쪽에 최종 결과를 보여준 뒤 매치를 정리한다."""
    if match.finished:
        return
    match.finished = True
    match.winner_id = winner_id
    match.end_reason = reason_key
    match.disarm()

    try:
        async with bot.pool.acquire() as conn:
            if reason_key:  # 시간 초과 등으로 중단된 경우
                await cancel_match(conn, match.match_id)
            else:
                await settle_match(conn, match.match_id, winner_id)
    except Exception as e:
        print(f"⚠️ 매치 정산 중 DB 오류: {e}")

    await match.broadcast()          # 웹 화면에 승패 연출을 먼저 띄운다
    live_matches.pop(match.match_id, None)

    # 방은 그대로 남는다 — 같은 사람들끼리 한 판 더 하려면 방을 다시 만들지 않아도 되게.
    # 결과를 잠깐 보여준 뒤(승패 연출을 읽을 시간) 대기실로 되돌린다.
    room = match.room
    if room is not None:
        room.ready.clear()
        room.last_active = time.time()
        if reason_key:
            room.last_result = {"key": "web_room_last_cancelled", "vars": {}}
        elif winner_id is None:
            room.last_result = {"key": "web_room_last_draw",
                                "vars": {"a": match.challenger.wins, "b": match.opponent.wins}}
        else:
            winner, loser = match.sides[winner_id], match.other(winner_id)
            room.last_result = {"key": "web_room_last_win",
                                "vars": {"winner": winner.user.display_name,
                                         "a": winner.wins, "b": loser.wins}}
        await room.broadcast()

    # 승패와 포인트 증감은 전부 웹 화면에서 보여준다 — 디스코드로는 아무것도 보내지 않는다.


async def start_round(match: PvpMatch) -> None:
    """다음 라운드 시작. 남은 카드가 1장뿐이면 고를 것도 없으니 자동으로 낸다.

    진행은 전부 웹 화면(match_page.html)에서 이뤄지므로 디스코드로는 아무것도 보내지 않는다.
    제한 시간은 서버 타이머가 재고, 남은 초는 스냅샷의 deadline을 보고 화면이 직접 그린다.
    """
    match.round_no += 1

    for side in (match.challenger, match.opponent):
        side.pick = None

    auto = [side for side in (match.challenger, match.opponent) if len(side.remaining) == 1]
    for side in auto:
        side.pick = side.remaining[0]

    if len(auto) == 2:  # 양쪽 다 마지막 카드 -> 바로 판정
        await resolve_round(match)
        return

    # 타이머에 **지금 라운드 번호를 같이 넘긴다.** 제한 시간이 끝나는 바로 그 순간에 카드를
    # 내면, 타이머가 이미 깨어난 뒤라 취소가 안 먹고 다음 라운드에 끼어들 수 있다.
    this_round = match.round_no      # 람다 안에서 읽으면 만료 시점 값이라 의미가 없다
    match.arm(config["pvp_config"]["round_pick_timeout_seconds"],
              lambda: round_timed_out(match, this_round))
    await match.broadcast()


async def round_timed_out(match: PvpMatch, round_no: int) -> None:
    """제한 시간 안에 안 낸 사람은 남은 카드 중 하나를 무작위로 대신 낸다 (매치를 취소하지 않음).

    `round_no`는 **이 타이머를 걸 때의 라운드 번호**다. 마감 직전에 카드를 내면 그 요청이
    라운드를 판정하고 다음 라운드를 시작하는데, 그 사이 타이머는 이미 깨어나 있어서
    `disarm()`이 못 막는다. 그대로 두면 **다음 라운드의 카드를 제멋대로 골라 즉시 판정해버린다**.
    번호가 달라졌으면 내 차례는 지난 것이므로 아무것도 하지 않는다.

    자동선택부터 판정까지 잠금을 놓지 않는다 — 중간에 놓으면 그 틈에 들어온 제출과 섞인다.
    """
    async with match.lock:
        if match.finished or match.round_no != round_no:
            return
        for side in (match.challenger, match.opponent):
            if side.pick is None and side.remaining:
                side.pick = random.choice(side.remaining)
        await match.broadcast()
        if match.challenger.pick and match.opponent.pick:
            await resolve_round(match)


async def deck_timed_out(match: PvpMatch) -> None:
    """제한 시간 안에 덱을 안 고르면 매치를 취소하고 판돈을 돌려준다.

    `round_no > 0`이면 마감 직전에 양쪽이 덱을 다 골라 1라운드가 이미 시작된 것이므로
    취소하면 안 된다 (그대로 두면 **막 시작된 매치의 판돈을 돌려주고 끊어버린다**).
    """
    async with match.lock:
        if match.finished or match.round_no > 0:
            return
        await finish_match(match, None, reason_key="match_cancelled_timeout")


async def resolve_round(match: PvpMatch) -> None:
    """양쪽 카드가 다 나왔을 때 승패를 계산하고 다음 라운드로 넘어간다."""
    a, b = match.challenger, match.opponent
    result = resolve_battle(a.pick, b.pick)
    if result["winner"] == "a":
        a.wins += 1
    elif result["winner"] == "b":
        b.wins += 1

    # 웹 화면이 "무슨 카드로 얼마가 나왔고 왜 그런지"를 연출할 수 있도록 판정 결과를 남긴다.
    # 카드가 remaining에서 빠지기 전에 담아야 한다.
    match.last_round = {
        "round": match.round_no,
        "challenger": {"card": web_card(a.pick), **result["detail_a"],
                       "won": result["winner"] == "a"},
        "opponent": {"card": web_card(b.pick), **result["detail_b"],
                     "won": result["winner"] == "b"},
        "draw": result["winner"] is None,
    }

    for side in (a, b):
        side.remaining = [c for c in side.remaining if c["hero_id"] != side.pick["hero_id"]]

    match.disarm()          # 이번 라운드 타이머 종료
    await match.broadcast()   # 판정 결과를 웹 화면에 먼저 띄운다

    # 판정 연출을 볼 시간을 준다. 이게 없으면 마지막 두 판이 붙어서 지나간다 —
    # 4라운드가 끝나면 남은 카드가 1장뿐이라 5라운드가 자동으로 즉시 진행되고,
    # 그 결과와 최종 승패까지 한꺼번에 튀어나와 무슨 일이 일어났는지 볼 수가 없다.
    await asyncio.sleep(config["pvp_config"].get("round_reveal_pause_seconds", 2))

    if a.remaining:
        await start_round(match)
    else:
        winner_id = (a.user.id if a.wins > b.wins
                     else b.user.id if b.wins > a.wins else None)
        await finish_match(match, winner_id)

# --- 로비와 방 ---------------------------------------------------------------
# 대결은 **전부 웹에서 시작한다.** 예전에는 디스코드에서 상대를 고르고 DM으로 도전장을 보냈는데,
# 그러면 (1) 상대가 접속해 있는지 알 수 없고 (2) DM이 막힌 사람에게는 도전 자체가 불가능했다.
# 지금은 디스코드 버튼이 로비 링크만 주고, 방을 만들고 들어가는 일은 로비 화면에서 한다.
#
# **상태는 전부 메모리에만 둔다.** 봇이 재시작되면 방이 사라지고 화면에는 안내가 뜬다 —
# 판돈을 "게임 시작" 시점에만 차감하므로 대기 중인 방이 날아가도 포인트 손해는 없다.

class Player:
    """대결 참가자의 최소 정보 (id + 표시 이름).

    `discord.User`를 그대로 들고 다니지 않는 이유: 진행이 전부 웹으로 옮겨가 디스코드로는
    아무것도 보내지 않고, 이름은 로비에 들어온 시점에 이미 받아뒀기 때문이다. 게다가 이 봇은
    members 인텐트가 없어 **나중에 id로 유저 객체를 되찾을 수 없다**(fetch_match_history 주석 참고).
    """

    __slots__ = ("id", "display_name")

    def __init__(self, user_id: int, display_name: str):
        self.id = user_id
        self.display_name = display_name


# 로비 입장권. 디스코드에서 카드게임 버튼을 누른 사람에게만 발급한다.
# 덱 링크(30분)보다 훨씬 길고, 접속해 있는 동안에는 계속 연장된다 — 로비는 무기한 열려 있어서
# 오래 머무는 게 정상이고, 대결 도중에 링크가 죽으면 그대로 몰수패가 되기 때문이다.
lobby_tokens: dict[str, dict] = {}       # token -> {user_id, name, lang, expires}
rooms: dict[int, "Room"] = {}            # room_id -> 방
lobby_sessions: set["LobbySession"] = set()
_room_seq = 0


def issue_lobby_token(user_id: int, name: str, lang: str) -> str:
    token = secrets.token_urlsafe(18)
    ttl = config["lobby_config"]["token_ttl_seconds"]
    lobby_tokens[token] = {"user_id": user_id, "name": name, "lang": lang,
                           "expires": time.time() + ttl}
    return token


def resolve_lobby_token(request) -> dict | None:
    """토큰을 확인하고 **유효기간을 연장한다**.

    연장하는 이유: 로비에 머무는 동안이나 한 판 하는 동안 링크가 만료되면 화면이 죽는다.
    반대로 창을 닫고 손을 떼면 그대로 만료되므로, 링크가 영원히 사는 것도 아니다.
    """
    entry = lobby_tokens.get(request.query.get("token", ""))
    if entry is None:
        return None
    if entry["expires"] < time.time():
        lobby_tokens.pop(request.query.get("token", ""), None)
        return None
    entry["expires"] = time.time() + config["lobby_config"]["token_ttl_seconds"]
    return entry


# **한 사람당 창 하나만 유지한다.** 탭마다 스트림과 대기열이 생기면 접속자 수와 메모리가
# 사람 수보다 부풀고, 어느 탭이 진짜인지도 알 수 없다. 새 창이 붙으면 옛 창을 끊는다
# (새 창을 막는 쪽이 아니라 — 옛 창은 이미 닫힌 좀비일 수 있다).
#
# **user_id만으로는 '다른 창'과 '같은 창의 재연결'을 구분할 수 없다.** SSE는 끊기면 브라우저가
# 알아서 다시 붙는데(프록시가 장시간 연결을 끊는 환경에서는 흔하다), 그걸 새 창으로 오해해서
# **창을 하나만 열었는데도 "다른 창에서 접속했습니다"가 떴다.** 그래서 페이지를 열 때 만든
# id(`cid`)를 같이 받아 **id가 다를 때만** 끊는다 — 같은 페이지가 다시 붙는 것은 그냥 교체한다.
live_streams: dict[int, tuple[str, asyncio.Queue, object]] = {}  # user_id -> (페이지 id, 대기열, 요청)
_KICK = object()                              # 대기열에 넣으면 "끊어라"라는 뜻


def client_id(request) -> str:
    return request.query.get("cid", "")


def close_other_streams(user_id: int, cid: str, path: str = "") -> None:
    """같은 사람의 **다른 페이지** 연결만 끊는다.

    조용히 넘어가야 하는 경우가 둘이다 —
    ① **같은 페이지의 재연결**(cid가 같다): SSE가 끊기면 브라우저가 알아서 다시 붙는데,
       그걸 새 창으로 오해하면 창 하나만 열었는데도 안내가 뜬다.
    ② **옛 연결이 이미 죽어 있는 경우**: 알릴 대상이 없는데 안내를 보내봐야 허공에 쏘는 것이고,
       그 사이 클라이언트가 새 연결을 맺었다면 엉뚱한 화면을 끊을 위험만 남는다.

    실제로 끊을 때는 **로그를 남긴다** — 운영에서 '안 열었는데 떴다'는 말이 나오면 로그의
    두 cid를 보고 정말 다른 페이지였는지 바로 가릴 수 있다.
    """
    entry = live_streams.get(user_id)
    if entry is None:
        return
    old_cid, queue, old_request = entry
    live_streams.pop(user_id, None)
    if old_cid == cid:
        return                      # ① 같은 페이지가 다시 붙은 것
    if old_request is not None and gone(old_request):
        return                      # ② 이미 끊긴 연결 — 조용히 교체한다
    print(f"⚠️ 같은 사람의 다른 창을 끊음 — uid={user_id} "
          f"옛cid={old_cid[:8] or '(없음)'} 새cid={cid[:8] or '(없음)'} 새경로={path}")
    try:
        queue.put_nowait(_KICK)
    except asyncio.QueueFull:
        pass


class LobbySession:
    """로비 화면 한 개의 접속. 한 사람당 하나만 살아 있다(`close_other_streams`)."""

    __slots__ = ("user_id", "name", "lang", "queue", "opened_at", "last_action")

    def __init__(self, user_id: int, name: str, lang: str):
        self.user_id = user_id
        self.name = name
        self.lang = lang
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self.opened_at = time.time()
        # **마지막으로 뭔가를 한 시각.** 연 시각이 아니라 이걸 기준으로 유휴를 잰다 —
        # 새로고침·방 만들기·입장 시도 같은 조작이 들어올 때마다 갱신된다(`touch_lobby`).
        self.last_action = time.time()


def touch_lobby(user_id: int) -> None:
    """그 사람의 로비 세션에 '방금 뭔가 했다'고 표시한다."""
    for session in lobby_sessions:
        if session.user_id == user_id:
            session.last_action = time.time()


BACKGROUND_RANDOM = "random"


def background_ids() -> list[str]:
    """설정에 등록된 배경 id 목록. 드롭박스 항목이자 **서버 검증의 기준**이다."""
    return [item["id"] for item in config["background_config"]["list"]]


def pick_background(choice: str) -> str:
    """방이 실제로 쓸 배경 하나를 정한다. `random`이면 그때그때 하나 고른다.

    랜덤을 **방을 만들 때 한 번** 고정하지 않는 이유: 그러면 랜덤과 직접 고른 것이
    구분되지 않는다. 대결이 시작될 때마다 다시 뽑아야 판마다 무대가 바뀐다.
    """
    ids = background_ids()
    if not ids:
        return ""
    if choice == BACKGROUND_RANDOM or choice not in ids:
        return random.choice(ids)
    return choice


def background_view(background_id: str) -> dict | None:
    """화면이 그대로 쓸 수 있는 배경 정보(주소 + 투명도)."""
    conf = config["background_config"]
    for item in conf["list"]:
        if item["id"] == background_id:
            # **파일명은 반드시 인코딩한다.** 버킷의 파일명이 한글 + 공백이라 그대로 붙이면
            # 공백이 주소를 끊고 한글도 서버마다 해석이 갈린다. id(ASCII)와 파일명을 따로
            # 둔 이유도 이것 — 버킷에서 이름을 바꿔도 드롭박스 값은 그대로 유지된다.
            filename = item.get("file") or f"{item['id']}{conf['ext']}"
            return {
                "id": item["id"],
                "url": conf["base_url"] + quote(filename),
                # 배경마다 밝기가 달라 투명도도 다르다 — 자세한 이유는 오락실.json 주석 참고
                "opacity": item["opacity"],
                "saturate": conf["saturate"],
            }
    return None


class Room:
    """대결 방 하나. 방장 + 상대 + 관전자, 그리고 시작되면 PvpMatch를 품는다."""

    def __init__(self, room_id: int, host: Player, name: str, wager: int, password: str, lang: str,
                 background: str = BACKGROUND_RANDOM):
        self.room_id = room_id
        self.name = name
        self.wager = wager
        # 방장이 고른 배경. "random"이면 고정하지 않고 판마다 다시 뽑는다.
        self.background = background
        self.current_background = pick_background(background)
        self.password = password          # ""면 공개 방
        self.lang = lang
        self.created_at = time.time()
        # 입장 순서를 유지한다 — 방장 승계와 상대 승격이 "먼저 들어온 사람" 순이다.
        self.members: dict[int, Player] = {host.id: host}
        # **방장(권한)과 대전자(자리)는 다른 개념이다.** 방장은 시작·추방 권한을 갖고,
        # 자리는 실제로 카드를 내는 두 사람이다. 방장도 관전으로 물러날 수 있어야 해서
        # 둘을 하나로 묶어두면 물러날 자리가 없다(예전에는 host_id가 곧 1번 대전자였다).
        self.host_id = host.id
        self.seats: list[int | None] = [host.id, None]
        self.ready: set[int] = set()
        # **스스로 관전으로 물러난 사람.** 이게 없으면 물러나자마자 `fill_seats`가 관전자
        # 맨 앞에서 그 사람을 도로 앉힌다(들어온 순서상 대개 본인이 맨 앞이다).
        # 직접 '앉기'를 누르면 풀린다.
        self.standing: set[int] = set()
        self.banned: set[int] = set()     # 추방당한 사람. 방이 닫힐 때까지 다시 못 들어온다.
        # 방에서 빠진 사람에게 **왜 빠졌는지** 한 번 알려주려고 남겨둔다 (스트림이 읽고 지운다).
        self.removed: dict[int, str] = {}
        self.invited: dict[int, float] = {}   # user_id -> 만료 시각 (비공개 방 비밀번호 면제)
        self.match: PvpMatch | None = None
        self.last_result: dict | None = None   # 지난 판 결과 (대기실에 한 줄로 남긴다)
        # 마지막으로 대결이 오간 시각. 이 뒤로 아무 대결도 없으면 방을 닫는다(room_janitor).
        self.last_active = time.time()
        # 지금 이 방 화면을 열어둔 사람들 (user_id -> 열려 있는 스트림 수).
        # 창을 닫으면 방에서 빼야 하는데, **새로고침도 잠깐 0이 되므로** 유예를 둔다.
        self.viewers: dict[int, int] = {}
        # 프록시가 SSE를 막아 **폴링으로 보는 사람**은 스트림이 없다. 그 사람까지 끊긴 것으로
        # 보면 멀쩡히 보고 있는데 방에서 빠지므로, 폴링 조회 시각도 '접속 중'으로 센다.
        self.polled: dict[int, float] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.closed = False
        self.close_reason: str | None = None
        # 그 사람에게 한 번만 보여줄 안내 (승격/추방 등). 스냅샷에 실어 보내고 지운다.
        # 값은 {"key": 문구키, "vars": {...}} 꼴이다 — 판돈 같은 숫자를 끼워 넣어야 해서.
        self.notices: dict[int, dict] = {}
        self.lock = asyncio.Lock()

    # -- 구성 ---------------------------------------------------------------
    @property
    def spectator_ids(self) -> list[int]:
        """자리에 앉지 않은 사람들. **입장 순서를 유지한다** (승격이 먼저 온 순이다)."""
        return [uid for uid in self.members if uid not in self.seats]

    @property
    def seated_ids(self) -> list[int]:
        return [uid for uid in self.seats if uid is not None]

    def seat_of(self, user_id: int) -> int | None:
        return self.seats.index(user_id) if user_id in self.seats else None

    @property
    def can_start(self) -> bool:
        """두 자리가 다 차고, **방장이 아닌 대전자가 전부 준비**했는가.

        방장은 시작 버튼을 누르는 쪽이라 따로 준비를 누르지 않는다. 방장이 관전 중이면
        두 대전자 모두 준비해야 한다.
        """
        if self.match is not None or None in self.seats:
            return False
        return all(uid in self.ready for uid in self.seated_ids if uid != self.host_id)

    @property
    def playing(self) -> bool:
        return self.match is not None and not self.match.finished

    def is_invited(self, user_id: int) -> bool:
        deadline = self.invited.get(user_id)
        return deadline is not None and deadline > time.time()

    # -- 스냅샷 -------------------------------------------------------------
    def list_entry(self) -> dict:
        """로비 방 목록에 실릴 요약."""
        host = self.members.get(self.host_id)
        return {
            "room_id": self.room_id,
            "name": self.name,
            "host": host.display_name if host else "",
            "seated": len(self.seated_ids),
            "wager": self.wager,
            "private": bool(self.password),
            "people": len(self.members),
            "playing": self.playing,
        }

    def seat_state(self, index: int) -> dict | None:
        uid = self.seats[index]
        if uid is None or uid not in self.members:
            return None
        return {
            "id": str(uid),
            "name": self.members[uid].display_name,
            "is_host": uid == self.host_id,
            # 방장은 준비를 누르지 않고 바로 시작하므로 항상 준비된 것으로 보여준다
            "ready": uid == self.host_id or uid in self.ready,
        }

    def snapshot(self, viewer_id: int) -> dict:
        """방 화면이 그릴 전체 상태. 대결이 시작되면 match 스냅샷을 통째로 품는다."""
        notice = self.notices.pop(viewer_id, None)
        return {
            "type": "room",
            "room_id": self.room_id,
            "name": self.name,
            "wager": self.wager,
            "private": bool(self.password),
            # 대기실과 대결이 같은 페이지라 배경도 방 스냅샷에 실어 보낸다.
            "background": background_view(self.current_background),
            "closed": self.closed,
            "close_reason": self.close_reason,
            "host_id": str(self.host_id),
            "seats": [self.seat_state(0), self.seat_state(1)],
            "spectators": [{"id": str(uid), "name": self.members[uid].display_name,
                            "is_host": uid == self.host_id}
                           for uid in self.spectator_ids],
            "me": {
                "id": str(viewer_id),
                "is_host": viewer_id == self.host_id,
                "seat": self.seat_of(viewer_id),
                "ready": viewer_id in self.ready,
            },
            "can_start": self.can_start,
            "notice": notice,
            "last_result": self.last_result,
            "match": self.match_snapshot(viewer_id),
        }

    def match_snapshot(self, viewer_id: int) -> dict | None:
        """진행 중인 판의 스냅샷. **관전자 수는 방 인원에서 센다.**

        예전에는 매치가 스트림 연결을 직접 세었는데, 지금은 구독이 방에 붙어 있어서
        매치는 자기 관전자가 몇 명인지 모른다. 방이 답을 갖고 있으므로 여기서 채워준다.
        """
        if self.match is None:
            return None
        snapshot = self.match.snapshot(viewer_id)
        snapshot["spectators"] = len(self.spectator_ids)
        return snapshot

    async def broadcast(self) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


def room_of(user_id: int) -> "Room | None":
    """그 사람이 지금 들어가 있는 방. 한 번에 하나만 허용한다."""
    for room in rooms.values():
        if not room.closed and user_id in room.members:
            return room
    return None


def online_user_ids() -> set[int]:
    """로비와 방을 통틀어 지금 접속해 있는 사람들 (탭이 여러 개여도 한 명)."""
    ids = {session.user_id for session in lobby_sessions}
    for room in rooms.values():
        if not room.closed:
            ids |= set(room.members)
    return ids


def lobby_base_snapshot() -> dict:
    """모두에게 똑같이 나가는 부분 (방 목록 + 접속자). **보는 사람과 무관하다.**

    **접속자 목록에는 로비에 있는 사람만 넣고, 인원 수는 방에 있는 사람까지 합쳐 센다** —
    목록은 "지금 부를 수 있는 사람"이고, 숫자는 "이 게임을 하고 있는 사람"이라 성격이 다르다.
    """
    in_lobby: dict[int, str] = {}
    for session in lobby_sessions:
        in_lobby.setdefault(session.user_id, session.name)
    online = online_user_ids()
    return {
        "type": "lobby",
        "online": len(online),
        "in_rooms": len(online) - len(in_lobby),
        "users": [{"id": str(uid), "name": name} for uid, name in in_lobby.items()],
        "rooms": [room.list_entry() for room in rooms.values() if not room.closed],
    }


def lobby_invites(viewer_id: int) -> list[dict]:
    """그 사람만 받는 부분 — 나를 부른 방들."""
    return [
        {"room_id": room.room_id, "name": room.name,
         "host": room.members[room.host_id].display_name if room.host_id in room.members else "",
         "wager": room.wager}
        for room in rooms.values()
        if not room.closed and room.is_invited(viewer_id) and viewer_id not in room.members
    ]


def lobby_snapshot(viewer_id: int) -> dict:
    """한 사람이 받을 전체 상태 (폴링과 첫 접속용)."""
    snapshot = lobby_base_snapshot()
    snapshot["invites"] = lobby_invites(viewer_id)
    snapshot["me"] = {"id": str(viewer_id)}
    return snapshot


# **로비 목록은 밀어주지 않는다 — 처음 들어올 때 한 번, 그 뒤로는 새로고침을 누를 때만 받는다.**
# 방이 생기고 사라질 때마다 접속자 전원에게 전체 목록을 보내면, 스냅샷 크기 자체가 접속자 수에
# 비례해 커져서 사실상 O(N^2)이 된다. 실측으로 200명이 붙은 상태에서 방 생성 20건이
# 로컬 CPU 1,047ms(= Render 0.1 CPU로 약 10초)를 먹었다. 목록이 조금 늦게 보이는 건
# 불편한 정도지만, 저 비용은 봇 전체를 멈추게 한다.
#
# **단, 초대만은 실시간으로 간다.** 새로고침을 눌러야 초대가 보인다면 초대 기능이 성립하지 않는다.
# 초대는 특정 한 사람에게만 가므로 전파가 아니라 1건짜리 전송이고, 접속자 수와 무관하다.
async def notify_invite(user_id: int, room: "Room") -> None:
    payload = json.dumps({
        "type": "invite",
        "room_id": room.room_id,
        "name": room.name,
        "host": room.members[room.host_id].display_name if room.host_id in room.members else "",
        "wager": room.wager,
    }, ensure_ascii=False)
    for session in list(lobby_sessions):
        if session.user_id != user_id:
            continue
        try:
            session.queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


_janitor: asyncio.Task | None = None


async def room_janitor() -> None:
    """주기적으로 **대결 없이 놀고 있는 방**을 닫는다.

    방은 메모리에만 있고 닫아주는 사람이 없으면 `max_rooms`(50)를 그대로 채운다 —
    창을 닫고 사라진 사람들의 빈 방이 목록을 막는다. 사람이 남아 있어도 **10분 동안
    대결이 한 번도 시작되지 않았으면** 닫는다(`room_idle_timeout_seconds`).
    대결이 시작되거나 끝날 때마다 `room.last_active`가 갱신되므로 계속 노는 방만 걸린다.

    **만료된 토큰과 낡은 기록도 같이 치운다.** 토큰은 쓸 때만 만료를 확인하므로, 다시 안 쓰는
    토큰은 그대로 남는다 — 하나가 200바이트 남짓이라 당장 문제는 아니지만 **영원히 늘어나는**
    자료구조라 오래 돌수록 쌓인다. 어차피 도는 청소부에 얹는 게 자연스럽다.
    """
    interval = config["lobby_config"].get("janitor_interval_seconds", 20)
    limit = config["lobby_config"].get("room_idle_timeout_seconds", 600)
    while True:
        await asyncio.sleep(interval)
        try:
            now = time.time()
            for room in list(rooms.values()):
                if room.closed or room.playing:
                    continue
                if now - room.last_active > limit:
                    await close_room(room, reason_key="web_room_closed_idle")
            sweep_expired(now)
        except Exception:
            traceback.print_exc()      # 청소부가 죽으면 방이 영영 안 닫힌다


def sweep_expired(now: float) -> None:
    """만료된 링크 토큰과 다 쓴 기록을 지운다."""
    for token, entry in list(lobby_tokens.items()):
        if entry["expires"] <= now:
            del lobby_tokens[token]
    for token, (_, _, expires) in list(deck_tokens.items()):
        if expires <= now:
            del deck_tokens[token]
    # 새로고침 간격만 보는 기록이라 그 시간이 지나면 남겨둘 이유가 없다
    cooldown = config["lobby_config"].get("refresh_cooldown_seconds", 5)
    for user_id, read_at in list(_lobby_reads.items()):
        if now - read_at > cooldown:
            del _lobby_reads[user_id]


def start_janitor() -> None:
    global _janitor
    if _janitor is None or _janitor.done():
        _janitor = asyncio.create_task(room_janitor())


async def close_room(room: Room, reason_key: str | None = None) -> None:
    """방을 닫고 모두를 내보낸다. 진행 중인 대결이 있으면 먼저 정리한다."""
    if room.closed:
        return
    room.closed = True
    room.close_reason = reason_key
    if room.match is not None and not room.match.finished:
        await finish_match(room.match, None, reason_key="match_cancelled_error")
    await room.broadcast()
    rooms.pop(room.room_id, None)


async def affordable(user_ids, wager: int) -> set[int]:
    """`wager`를 낼 수 있는 사람들. 친선전(0)이면 전원 통과 (DB도 안 본다)."""
    user_ids = list(user_ids)
    if wager <= 0 or not user_ids:
        return set(user_ids)
    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, points FROM users WHERE user_id = ANY($1::bigint[])", user_ids)
    return {row["user_id"] for row in rows if row["points"] >= wager}


async def fill_seats(room: Room) -> None:
    """빈 자리를 관전자 중에서 채운다 (들어온 순서대로).

    **판돈전이면 판돈을 낼 수 있는 사람만** 올린다 — 못 내는 사람을 올려두면 방장이 시작을
    눌러도 계속 실패해서 방이 멈춘다. 친선전은 조건이 없으므로 맨 앞 사람이 올라간다.
    준비 버튼을 눌러야 대결이 시작되므로, 올라간 사람이 원치 않으면 그냥 안 누르면 된다.
    """
    if room.playing or None not in room.seats:
        return
    candidates = [uid for uid in room.spectator_ids if uid not in room.standing]
    if not candidates:
        return
    allowed = await affordable(candidates, room.wager)
    queue = [uid for uid in candidates if uid in allowed]
    for index, uid in enumerate(room.seats):
        if uid is None and queue:
            taker = queue.pop(0)
            room.seats[index] = taker
            room.ready.discard(taker)
            room.notices[taker] = {"key": "web_room_became_opponent", "vars": {}}


async def recheck_seats(room: Room) -> None:
    """판돈을 더는 못 내는 대전자를 관전자로 내리고 빈 자리를 다시 채운다.

    한 판이 끝나면 진 쪽 포인트가 줄어 **다음 판을 시작할 수 없는 상태**가 될 수 있다.
    그대로 두면 방장이 시작을 눌러도 계속 실패해 방이 멈추므로, 낼 수 있는 관전자와 바꾼다.
    방장이 자리에서 내려와도 **방장 권한은 그대로**다 (자리와 권한은 별개다).
    """
    if room.playing or room.wager <= 0:
        return
    seated = room.seated_ids
    if not seated:
        return
    allowed = await affordable(seated, room.wager)
    for index, uid in enumerate(room.seats):
        if uid is not None and uid not in allowed:
            room.seats[index] = None
            room.ready.discard(uid)
            room.notices[uid] = {"key": "web_room_demoted_poor", "vars": {"wager": room.wager}}
    await fill_seats(room)


async def drop_disconnected(room: Room, user_id: int) -> None:
    """방 화면을 닫은 사람을 방에서 뺀다 (유예 시간이 지난 뒤에 확인한다).

    **대결 중이면 그 판이 끝날 때까지 기다렸다가 뺀다** — 카드를 내다 만 사람을 즉시 빼면
    판이 성립하지 않는다(남은 카드는 `round_timed_out`이 자동으로 내주므로 대결은 끝까지 간다).
    대결 중이 아니면 바로 뺀다 — 안 그러면 창만 닫고 사라진 사람 때문에 자리와 방이 계속 묶인다.

    유예(`disconnect_grace_seconds`)가 필요한 이유: **새로고침도 스트림이 한 번 끊긴다.**
    유예 없이 빼면 F5 한 번에 방 밖으로 튕긴다.
    """
    grace = config["lobby_config"].get("disconnect_grace_seconds", 5)

    def still_here() -> bool:
        # 스트림이 다시 붙었거나(새로고침) 폴링으로 계속 보고 있으면 접속 중이다
        return bool(room.viewers.get(user_id)
                    or time.time() - room.polled.get(user_id, 0) < grace * 2)

    await asyncio.sleep(grace)
    if room.closed or still_here():
        return                                  # 다시 들어왔다 (새로고침이었다)
    while room.playing and user_id in room.seats:
        # 진행 중인 판이 끝날 때까지 기다린다. 자동선택으로 어차피 끝까지 간다.
        await asyncio.sleep(2)
        if room.closed or still_here():
            return
    async with room.lock:
        await leave_room(room, user_id)


async def leave_room(room: Room, user_id: int) -> None:
    """방에서 한 사람을 뺀다. 방장/상대가 빠지면 자리를 메운다.

    **대결 중 이탈은 대결을 끊지 않는다** — 덱을 고르기 전이면 무효 + 판돈 반환이고,
    덱을 고른 뒤에는 남은 카드가 자동으로 나가면서 그대로 끝까지 진행된다(round_timed_out).
    창을 닫아 판돈을 피하는 걸 막으려면 진행을 계속시키는 쪽이 맞다.
    """
    if user_id not in room.members:
        return
    was_player = user_id in room.seats
    room.members.pop(user_id, None)
    room.ready.discard(user_id)
    room.standing.discard(user_id)
    seat = room.seat_of(user_id)
    if seat is not None:
        room.seats[seat] = None

    if room.playing and was_player and room.match.round_no == 0:
        # 아직 덱을 아무도 안 골랐다 — 여기서 끊어야 판돈이 묶인 채 남지 않는다
        await finish_match(room.match, None, reason_key="match_cancelled_timeout")

    if not room.members:
        await close_room(room)
        return

    if user_id == room.host_id:
        # 방장 승계: 남은 대전자 -> 없으면 가장 먼저 들어온 사람
        remaining = room.seated_ids
        room.host_id = remaining[0] if remaining else next(iter(room.members))
        room.ready.discard(room.host_id)      # 방장은 준비를 누르지 않는다
        room.notices[room.host_id] = {"key": "web_room_became_host", "vars": {}}

    if not room.playing:
        await fill_seats(room)
    await room.broadcast()


# 오락실 패널 — 슬래시 명령 대신 채널에 고정된 버튼 메시지로 모든 기능을 연다.
# 통합관리봇의 음성채널 생성 패널과 같은 방식: 봇이 켜질 때 기존 메시지를 지우고 새로 올린다.
# ---------------------------------------------------------------------------

class ArcadePanelView(discord.ui.View):
    """채널에 상주하는 기능 버튼 모음.

    `timeout=None` + 버튼마다 고정 `custom_id`라 봇이 재시작해도 눌리는 영구 View다.
    재시작 때 메시지를 새로 올리긴 하지만, 지우기에 실패해 옛 메시지가 남아도
    `bot.add_view()`로 등록해두면 그 버튼도 계속 동작한다.
    """

    def __init__(self, lang: str):
        super().__init__(timeout=None)
        self.lang = lang
        # **줄은 직접 나눈다.** 디스코드는 한 줄에 버튼 5개까지라, 6개를 그냥 넣으면
        # discord.py가 5 + 1로 채워서 마지막 버튼(포인트) 하나만 덩그러니 아랫줄에 떨어진다.
        # 3 + 3으로 나눠 "매일 하는 것 / 관리·대결"이 줄로도 구분되게 한다.
        for row, key, style, handler in (
            (0, "panel_checkin", discord.ButtonStyle.success, self.on_checkin),
            (0, "panel_summon", discord.ButtonStyle.primary, self.on_summon),
            (0, "panel_summon_multi", discord.ButtonStyle.primary, self.on_summon_multi),
            (1, "panel_deck", discord.ButtonStyle.secondary, self.on_deck),
            (1, "panel_arcade", discord.ButtonStyle.danger, self.on_arcade),
            (1, "panel_points", discord.ButtonStyle.secondary, self.on_points),
        ):
            label = get_msg(lang, key, count=config["summon_config"]["multi_count"])                 if key == "panel_summon_multi" else get_msg(lang, key)
            button = discord.ui.Button(label=label, style=style, row=row,
                                       custom_id=f"arcade_{key}_{lang}")
            button.callback = handler
            self.add_item(button)

    async def on_checkin(self, interaction: discord.Interaction):
        await do_checkin(interaction)

    async def on_points(self, interaction: discord.Interaction):
        await do_points(interaction)

    async def on_deck(self, interaction: discord.Interaction):
        await do_deck(interaction)

    async def on_summon(self, interaction: discord.Interaction):
        conf = config["summon_config"]
        await summon_and_reply(interaction, 1, conf["cost_single"])

    async def on_summon_multi(self, interaction: discord.Interaction):
        conf = config["summon_config"]
        await summon_and_reply(interaction, conf["multi_count"], conf["cost_multi"])

    async def on_arcade(self, interaction: discord.Interaction):
        await do_arcade(interaction)


async def setup_arcade_panel(guild: discord.Guild) -> int:
    """설정에 적힌 카테고리/채널마다 기능 버튼 메시지를 새로 올린다.

    봇이 켜질 때마다 기존 봇 메시지를 지우고 다시 올려서, 패널이 항상 채널 맨 아래에 오고
    버튼 문구나 구성이 바뀌어도 자동으로 반영된다 (통합관리봇 음성채널 패널과 같은 방식).
    """
    posted = 0
    for category_name, conf in config.get("panel_config", {}).get("categories", {}).items():
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            continue
        channel = discord.utils.get(category.text_channels, name=conf["channel"])
        if channel is None:
            continue

        lang = conf["lang"]
        try:
            async for message in channel.history(limit=30):
                if message.author == bot.user:
                    await message.delete()
            embed = discord.Embed(title=get_msg(lang, "panel_title"),
                                  description=get_msg(lang, "panel_desc"),
                                  color=discord.Color.blurple())
            await channel.send(embed=embed, view=ArcadePanelView(lang))
            posted += 1
        except discord.HTTPException as e:
            print(f"⚠️ 오락실 패널 게시 실패 ({guild.name} / {channel.name}): {e}")

    return posted


def main():
    token = os.environ["BOT_TOKEN"]
    bot.run(token)

if __name__ == "__main__":
    main()
