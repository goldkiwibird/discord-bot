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
# hero_base_stats 뷰(직업 기본 스탯 + 종족 보정)와 user_cards의 bonus_* 컬럼이 쓰는 스탯 5종
STAT_KEYS = ("attack", "hp", "defense", "accuracy", "evasion")

def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# 슬래시 명령어 데코레이터가 모듈 로드 시점에 바로 실행되기 때문에,
# on_ready가 아니라 여기서 동기적으로 한 번 읽어들인다.
config = load_config()

def today_kst() -> date:
    return datetime.now(KST).date()

def resolve_lang(interaction: discord.Interaction) -> str:
    """명령어를 입력한 채널의 카테고리로 표시 언어를 정한다 (유저의 디스코드 클라이언트 언어가 아님).

    통합관리봇의 카테고리별 자동번역과 같은 방식 — 영어/번체 채널 카테고리에서 명령어를 쓰면
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
    if lang == "zh-TW":
        return hero.get("name_zh_tw") or hero["name"]
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

    영웅 마스터 데이터(heroes / job_base_stats / race_stat_bonus / hero_base_stats)는
    hero-cards 폴더의 SQL로 따로 넣는 자료라 여기서 만들지 않는다.
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

class ArcadeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool: asyncpg.Pool | None = None
        # 54종 고정 데이터라 시작할 때 한 번만 읽어서 등급별로 묶어둔다 (소환할 때마다 조회하지 않음)
        self.heroes_by_grade: dict[str, list[dict]] = {}

    async def setup_hook(self):
        database_url = os.environ["DATABASE_URL"]
        self.pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

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
        try:
            rows = await conn.fetch(
                "SELECT id, name, name_en, name_zh_tw, grade, job, element, "
                "attack, hp, defense, accuracy, evasion FROM hero_base_stats"
            )
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as e:
            # 영웅 마스터 데이터(또는 이름 번역 컬럼)가 아직 Neon에 안 들어간 상태.
            # 소환만 막고 출석/포인트는 그대로 쓸 수 있게 둔다.
            self.heroes_by_grade = {}
            print(f"⚠️ {type(e).__name__}: 소환 기능이 비활성화됩니다. "
                  f"hero-cards의 heroes.sql / job_race_stats.sql / hero_name_translations.sql을 "
                  f"Neon에 순서대로 반영하세요.")
            return

        self.heroes_by_grade = {}
        for row in rows:
            self.heroes_by_grade.setdefault(row["grade"], []).append(dict(row))
        print(f"영웅 데이터 {len(rows)}종 로드: " + ", ".join(f"{g} {len(v)}종" for g, v in self.heroes_by_grade.items()))

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
        stat = min(STAT_KEYS, key=lambda key: me[key])
    elif rule == "BOTH_HIGHEST":  # 사제 vs 사제는 각자 자기 최고 스탯
        stat = max(STAT_KEYS, key=lambda key: me[key])
    elif rule == "MATCH_OPP_LOWEST":
        # 사제는 상대의 최저 스탯과 같은 카테고리를 쓴다.
        # 최저가 여러 개면 그중 사제 본인 수치가 가장 높은 것을 고른다 (상대 수치는 어느 쪽이든 동일).
        lowest = min(opp[key] for key in STAT_KEYS)
        stat = max((key for key in STAT_KEYS if opp[key] == lowest), key=lambda key: me[key])
    else:
        stat = rule
    return {"value": me[stat], "stat": stat, "rule": rule}

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

async def create_match(conn, challenger_id: int, opponent_id: int, wager: int) -> tuple[int | None, str | None]:
    """도전장 기록만 만든다 (판돈은 아직 안 뺌 — 상대가 수락해야 뺀다).

    (match_id, None) 또는 (None, 실패사유)를 돌려준다.
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
        "INSERT INTO pvp_matches (challenger_id, opponent_id, wager, status) "
        "VALUES ($1, $2, $3, 'pending') RETURNING match_id",
        challenger_id, opponent_id, wager,
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
    rows = await conn.fetch(
        """
        SELECT c.hero_id, c.enhance_count,
               c.bonus_attack, c.bonus_hp, c.bonus_defense, c.bonus_accuracy, c.bonus_evasion,
               h.name, h.name_en, h.name_zh_tw, h.grade, h.job, h.element,
               h.attack, h.hp, h.defense, h.accuracy, h.evasion,
               COALESCE((SELECT array_agg(d.deck_number ORDER BY d.deck_number)
                         FROM user_decks d
                         WHERE d.user_id = c.user_id AND d.hero_id = c.hero_id),
                        '{}') AS deck_numbers
        FROM user_cards c
        JOIN hero_base_stats h ON h.id = c.hero_id
        WHERE c.user_id = $1
        ORDER BY h.grade, h.id
        """,
        user_id,
    )

    cards = []
    for row in rows:
        hero = dict(row)
        bonus = {key: row[f"bonus_{key}"] for key in STAT_KEYS}
        cards.append({
            "hero_id": row["hero_id"],
            "name": hero_display_name(hero, lang),
            "image_name": row["name"],  # 이미지 파일명은 항상 한글 원본
            "grade": row["grade"],
            "job": row["job"],
            "element": row["element"],  # 필터용 (속성/등급/직업)
            # 보유 카드 화면이라 강화분이 반영된 현재 스탯 + 증가분을 같이 내려준다 ("10 (+3)" 표기용)
            "stats": {key: row[key] + bonus[key] for key in STAT_KEYS},
            "bonus": bonus,
            "enhance_count": row["enhance_count"],
            # 같은 카드가 여러 덱에 들어갈 수 있으므로 목록으로 내려준다.
            # LEFT JOIN으로 두면 덱 수만큼 행이 늘어나 같은 카드가 화면에 여러 번 뜬다.
            "deck_numbers": list(row["deck_numbers"]),
        })

    return {"cards": cards}

async def fetch_summon_history(conn, user_id: int, lang: str, limit: int) -> list[dict]:
    """소환 이력. 최근 것부터 `limit`건 (1건 = 소환 1회, 10연이면 카드 10장이 한 건에 들어 있음).

    영웅 이름/등급은 로그에 복사해 넣지 않고 `hero_base_stats`에서 조회한다 — 복사해 두면
    나중에 마스터 데이터를 고쳤을 때 과거 기록만 옛 값으로 남아 어긋난다.
    `bot.heroes_by_grade`(메모리 로스터)를 쓰지 않는 이유는 봇이 켜진 상태에만 의존하게 되어
    조회 함수를 단독으로 검증할 수 없고, 로스터가 비어 있으면 카드가 조용히 사라지기 때문이다.
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

    # 등장한 영웅만 한 번에 조회해서 id -> 정보로 만든다 (행마다 조인하지 않도록)
    hero_ids = {hero_id for row in rows for hero_id in row["hero_ids"]}
    heroes = {
        hero["id"]: dict(hero)
        for hero in await conn.fetch(
            "SELECT id, name, name_en, name_zh_tw, grade, job, element "
            "FROM hero_base_stats WHERE id = ANY($1::int[])",
            list(hero_ids),
        )
    }

    history = []
    for row in rows:
        cards = []
        for hero_id, outcome in zip(row["hero_ids"], row["outcomes"]):
            hero = heroes.get(hero_id)
            if hero is None:      # 마스터 데이터에서 빠진 영웅 — 이름을 못 찾아도 기록은 보여준다
                cards.append({"name": f"#{hero_id}", "image_name": "", "grade": "R",
                              "job": "warrior", "element": "", "outcome": outcome})
                continue
            cards.append({
                "name": hero_display_name(hero, lang),
                "image_name": hero["name"],   # 이미지 파일명은 항상 한글 원본
                "grade": hero["grade"],
                "job": hero["job"],           # 웹에서 카드 그림을 조립할 때 직업/속성 아이콘이 필요하다
                "element": hero["element"],
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

    `pvp_matches`에 이미 쌓여 있어서 새 테이블이 필요 없다. 상대 이름은 DB에 없고(디스코드에만 있음)
    봇이 캐시에서 못 찾을 수도 있으므로, 이름 해석은 이 함수가 아니라 호출하는 쪽에 맡긴다.
    """
    rows = await conn.fetch(
        """
        SELECT match_id, challenger_id, opponent_id, wager, status, winner_id, created_at
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
        them = row["opponent_id"] if row["challenger_id"] == user_id else row["challenger_id"]
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
            "result": result,
            "wager": row["wager"],
            "delta": delta,
            "played_at": row["created_at"].isoformat(),
        })
    return history

async def fetch_battle_decks(conn, user_id: int, lang: str) -> dict[int, list[dict]]:
    """PVP에 쓸 수 있는 덱(5장이 다 채워진 것)만 카드 정보까지 붙여서 가져온다."""
    rows = await conn.fetch(
        """
        SELECT d.deck_number, d.slot, d.hero_id,
               h.name, h.name_en, h.name_zh_tw, h.grade, h.job, h.element,
               h.attack, h.hp, h.defense, h.accuracy, h.evasion,
               c.bonus_attack, c.bonus_hp, c.bonus_defense, c.bonus_accuracy, c.bonus_evasion
        FROM user_decks d
        JOIN hero_base_stats h ON h.id = d.hero_id
        JOIN user_cards c ON c.user_id = d.user_id AND c.hero_id = d.hero_id
        WHERE d.user_id = $1
        ORDER BY d.deck_number, d.slot
        """,
        user_id,
    )

    decks: dict[int, list[dict]] = {}
    for row in rows:
        # PVP는 유저가 실제로 보유한 카드로 싸우므로 강화분이 반영된 현재 스탯을 쓴다
        card = {
            "hero_id": row["hero_id"],
            "name": row["name"],  # 이미지 파일명용 한글 원본
            "display_name": hero_display_name(dict(row), lang),
            "grade": row["grade"],
            "job": row["job"],
            "element": row["element"],
            "bonus": {key: row[f"bonus_{key}"] for key in STAT_KEYS},
        }
        card.update({key: row[key] + card["bonus"][key] for key in STAT_KEYS})
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
    with open("deck_page.html", "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

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
                    "web_expired")
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

    # 상대 이름은 디스코드 쪽에만 있다. 캐시에 없으면 ID를 그대로 보여주는 대신 빈 값으로 두고
    # 화면에서 "알 수 없는 상대"로 표기한다 (여기서 사용자 조회 API를 호출하면 느려진다).
    for match in matches:
        user = bot.get_user(int(match["opponent_id"]))
        match["opponent_name"] = user.display_name if user else ""

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

        database = False
        if bot.pool is not None:
            try:
                database = await asyncio.wait_for(probe_db(), timeout) == 1
            except (asyncpg.PostgresError, OSError, asyncio.TimeoutError):
                database = False

        discord_ok = bot.is_ready() and not bot.is_closed()
        healthy = discord_ok and database
        return web.json_response(
            {
                "status": "ok" if healthy else "degraded",
                "discord": discord_ok,
                "database": database,
                # 연결 전에는 latency가 NaN이라 그대로 넣으면 JSON으로 직렬화할 수 없다
                "latency_ms": None if math.isnan(bot.latency) else round(bot.latency * 1000),
            },
            status=200 if healthy else 503,
        )
    except Exception as e:
        traceback.print_exc()
        return web.json_response({"status": "error", "error": type(e).__name__}, status=503)

def resolve_match_view(request) -> "tuple[PvpMatch, int | None] | None":
    """요청이 가리키는 매치와 '보는 사람'을 찾는다.

    토큰이 있으면 그 참가자 관점(아래쪽이 본인, 카드 제출 가능), 없으면 관전자(읽기 전용).
    관전은 링크만 있으면 되므로 토큰을 요구하지 않는다 — 양쪽 덱은 1라운드 시작과 동시에
    규칙상 전부 공개되는 정보라 숨길 게 없다.
    """
    try:
        match_id = int(request.query.get("id", ""))
    except ValueError:
        return None
    match = live_matches.get(match_id)
    if match is None:
        return None

    entry = match_tokens.get(request.query.get("token", ""))
    if entry and entry[0] == match_id:
        return match, entry[1]
    return match, None


async def handle_match_page(request):
    with open("match_page.html", "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")


async def handle_match_state(request):
    """SSE가 막혔을 때 쓰는 폴링용 단발 조회 (브라우저가 자동으로 이쪽으로 내려온다)."""
    resolved = resolve_match_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    match, viewer_id = resolved
    return web.json_response(match.snapshot(viewer_id))


async def handle_match_labels(request):
    """대전 화면이 처음 한 번만 받아가는 정적 자료 (문구·라벨·이미지 주소).

    상태 스냅샷에 같이 넣으면 이벤트마다 1.5KB씩 따라다니므로 따로 뺐다.
    언어는 매치에 기록된 값을 쓴다(도전자가 명령어를 실행한 채널 언어).
    """
    resolved = resolve_match_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    match, _ = resolved
    lang = match.lang

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
                "web_match_round", "web_match_score", "web_match_win", "web_match_lose",
                "web_match_deck_prompt", "web_match_deck_label",
                "match_cancelled_timeout", "match_cancelled_error",
                "web_match_draw", "web_match_final_win", "web_match_final_lose",
                "web_match_final_draw", "web_match_ended", "web_match_lost_connection",
                "web_match_not_found", "web_match_wager",
                "pvp_reason_own_lowest", "pvp_reason_both_highest", "pvp_reason_match_opp_lowest",
                "pvp_reason_direct", "pvp_reason_element_cycle", "pvp_reason_element_light",
                "pvp_reason_element_basic", "pvp_reason_element_dark",
            )
        },
    })


async def handle_match_stream(request):
    """매치 상태를 실시간으로 밀어주는 SSE 스트림.

    Cloudflare/nginx가 응답을 모아뒀다 내보내면 실시간성이 깨지므로 `X-Accel-Buffering: no`를
    붙이고, 유휴 연결이 끊기지 않도록 주기적으로 주석 하트비트(`: ping`)를 보낸다.
    그래도 막히는 환경이 있을 수 있어 클라이언트는 폴링으로 내려갈 수 있게 해뒀다.
    """
    resolved = resolve_match_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    match, viewer_id = resolved

    is_player = viewer_id in match.sides
    if not is_player and match.spectators >= config["pvp_config"].get("max_spectators", 50):
        return web.json_response({"error": "spectators_full"}, status=429)

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    await response.prepare(request)

    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    match.subscribers.add(queue)
    if is_player:
        match.web_sides.add(viewer_id)
    else:
        match.spectators += 1
        await match.broadcast()      # 관전자 수가 바뀌었으니 모두에게 알린다

    heartbeat = config["pvp_config"].get("stream_heartbeat_seconds", 15)
    try:
        await response.write(_sse(match.snapshot(viewer_id)))
        while not match.finished or not queue.empty():
            try:
                await asyncio.wait_for(queue.get(), heartbeat)
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")   # 버퍼를 밀어내고 연결도 유지
                continue
            await response.write(_sse(match.snapshot(viewer_id)))
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        match.subscribers.discard(queue)
        if is_player:
            match.web_sides.discard(viewer_id)
        else:
            match.spectators = max(0, match.spectators - 1)
            if not match.finished:
                await match.broadcast()
    return response


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def handle_match_deck(request):
    """웹에서 이번 대결에 쓸 덱을 고른다. 양쪽이 다 고르면 1라운드가 시작된다."""
    resolved = resolve_match_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    match, viewer_id = resolved
    if viewer_id not in match.sides:
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

    await match.broadcast()

    async with match.lock:
        them = match.other(viewer_id)
        if them.deck_number is not None and not match.finished:
            await run_match_step(match, start_round(match))
    return web.json_response({"ok": True})


async def handle_match_pick(request):
    """웹에서 카드를 낸다. 디스코드 선택 메뉴와 **같은 경로를 타야** 경합이 안 난다."""
    resolved = resolve_match_view(request)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    match, viewer_id = resolved
    if viewer_id not in match.sides:
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

    await match.broadcast()

    async with match.lock:
        them = match.other(viewer_id)
        if them.pick is not None and not match.finished:
            await run_match_step(match, resolve_round(match))
    return web.json_response({"ok": True})


async def start_web_server():
    """Render는 웹 서비스가 PORT를 열고 있어야 해서, 헬스체크 겸 덱 편성 페이지를 여기서 서빙한다.

    Render 무료 플랜은 들어오는 요청이 한동안 없으면 인스턴스를 재우고(스핀다운), 그동안엔
    봇도 같이 멈춘다. 그래서 UptimeRobot 같은 외부 모니터가 여기를 주기적으로 찔러줘야 한다.
    """
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="Arcade bot is online!"))
    app.router.add_get("/health", handle_health)
    app.router.add_get("/deck", handle_deck_page)
    app.router.add_get("/api/deck", handle_deck_data)
    app.router.add_get("/api/history", handle_match_history)
    app.router.add_get("/api/summons", handle_summon_history)
    app.router.add_get("/match", handle_match_page)
    app.router.add_get("/api/match", handle_match_state)
    app.router.add_get("/api/match/labels", handle_match_labels)
    app.router.add_get("/api/match/stream", handle_match_stream)
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
    url = f"{web_base_url()}/deck?token={token}"
    minutes = config["web_config"]["token_ttl_seconds"] // 60

    embed = discord.Embed(
        title=get_msg(lang, "deck_link_title"),
        description=f"{get_msg(lang, 'deck_link_desc', minutes=minutes)}\n\n{url}",
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- PVP 진행 ---------------------------------------------------------------
# 매치 진행 상태는 메모리에만 둔다. 봇이 재시작되면 버튼(View)이 어차피 죽어서 이어갈 수 없고,
# 묶여 있던 판돈은 시작할 때 refund_stale_matches()가 되돌려준다.

class PvpSide:
    """한 참가자 쪽 상태 (덱, 남은 카드, 이번 라운드에 낸 카드, DM 메시지)."""

    def __init__(self, user: discord.User, decks: dict[int, list[dict]]):
        self.user = user
        self.decks = decks
        self.deck_number: int | None = None
        self.remaining: list[dict] = []
        self.pick: dict | None = None
        self.wins = 0
        self.message: discord.Message | None = None  # 계속 갱신할 DM 메시지

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
        # 공개 대결이면 공개매치 채널에 올라간 그 매치의 메시지. 도전장 -> 진행 중 -> 종료로
        # **한 메시지를 계속 고쳐 쓴다** (매치마다 메시지가 세 개씩 쌓이지 않게).
        self.public_message = None
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
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.deadline = None
        self.web_sides: set[int] = set()      # 웹 화면을 열어둔 참가자 id

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

        def side_state(side: PvpSide, reveal: bool) -> dict:
            # 덱을 아직 안 골랐으면 카드가 없다. 고른 뒤에는 양쪽 덱이 규칙상 전부 공개된다.
            cards = [web_card(c) for c in side.remaining] if reveal else []
            return {
                "name": side.user.display_name,
                "wins": side.wins,
                "deck_chosen": side.deck_number is not None,
                "remaining": cards,
                "remaining_count": len(side.remaining),
                "picked": side.pick is not None,
                "pick": web_card(side.pick) if (side.pick and self.last_round) else None,
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
            "me": side_state(me, both_chose),
            "them": side_state(them, both_chose),
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

active_matches: dict[int, PvpMatch] = {}  # user_id -> 참여 중인 매치
live_matches: dict[int, PvpMatch] = {}   # match_id -> 매치 (웹 관전/조작이 id로 찾는다)
# 웹에서 카드를 낼 수 있는 권한. 관전 링크는 토큰이 없어 읽기 전용이 된다.
match_tokens: dict[str, tuple[int, int]] = {}   # token -> (match_id, user_id)


def issue_match_token(match_id: int, user_id: int) -> str:
    token = secrets.token_urlsafe(18)
    match_tokens[token] = (match_id, user_id)
    return token


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
    for token, (mid, _) in list(match_tokens.items()):
        if mid == match.match_id:
            del match_tokens[token]

    # 공개 대결이면 채널에 남아 있는 메시지를 '종료'로 바꾼다. 안 그러면 끝난 매치가
    # 계속 '진행 중'으로 보이고, 이미 죽은 관전 링크를 누르게 된다.
    if match.public_message is not None:
        if reason_key:
            detail = get_msg(match.lang, reason_key)
        elif winner_id is None:
            detail = get_msg(match.lang, "match_public_result_draw",
                             a=match.challenger.wins, b=match.opponent.wins)
        else:
            detail = get_msg(match.lang, "match_public_result_win",
                             winner=match.sides[winner_id].user.display_name,
                             a=match.challenger.wins, b=match.opponent.wins)
        await edit_public_match(match.public_message, match.lang, match.challenger.user,
                                match.opponent.user, match.wager,
                                state="ended", detail=detail)

    # 승패와 포인트 증감은 전부 웹 화면에서 보여준다 — 디스코드로는 아무것도 보내지 않는다.
    for side in (match.challenger, match.opponent):
        active_matches.pop(side.user.id, None)


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

    match.arm(config["pvp_config"]["round_pick_timeout_seconds"], lambda: round_timed_out(match))
    await match.broadcast()


async def round_timed_out(match: PvpMatch) -> None:
    """제한 시간 안에 안 낸 사람은 남은 카드 중 하나를 무작위로 대신 낸다 (매치를 취소하지 않음)."""
    if match.finished:
        return
    async with match.lock:
        if match.finished:
            return
        for side in (match.challenger, match.opponent):
            if side.pick is None and side.remaining:
                side.pick = random.choice(side.remaining)
    await match.broadcast()
    async with match.lock:
        if not match.finished and match.challenger.pick and match.opponent.pick:
            await resolve_round(match)


async def deck_timed_out(match: PvpMatch) -> None:
    """제한 시간 안에 덱을 안 고르면 매치를 취소하고 판돈을 돌려준다."""
    if not match.finished:
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

def public_match_channel(channel) -> "discord.TextChannel | None":
    """공개 대결을 올릴 채널 — 패널을 누른 채널이 **아니라 같은 카테고리의 공개매치 채널**이다.

    패널 채널(🕹️오락실)에 도전장과 관전 링크가 쌓이면 버튼 패널이 위로 밀려 올라가
    정작 기능을 쓰기 어려워진다. 그래서 언어 카테고리마다 따로 둔 채널
    (`panel_config.categories[*].match_channel`)로 보낸다.
    """
    category = getattr(channel, "category", None)
    if category is None:
        return None
    conf = config.get("panel_config", {}).get("categories", {}).get(category.name, {})
    name = conf.get("match_channel")
    return discord.utils.get(category.text_channels, name=name) if name else None


def public_match_channel_name(channel) -> str:
    """설정에 적힌 공개매치 채널 이름 (안내 문구용 — 채널이 실제로 없어도 이름은 알려준다)."""
    category = getattr(channel, "category", None)
    conf = config.get("panel_config", {}).get("categories", {}).get(
        getattr(category, "name", ""), {})
    return conf.get("match_channel", "")


def public_match_embed(lang: str, challenger, opponent, wager: int, *,
                       state: str, url: str = "", detail: str = "") -> discord.Embed:
    """공개매치 채널에 올라가는 매치 카드. `state`는 live(진행 중) / ended(종료)."""
    lines = [
        get_msg(lang, "match_public_players",
                challenger=challenger.display_name, opponent=opponent.display_name),
        get_msg(lang, "match_public_wager", wager=wager) if wager
        else get_msg(lang, "match_public_friendly"),
    ]
    if detail:
        lines.append(detail)
    if url:
        lines.append(get_msg(lang, "match_public_spectate", url=url))
    return discord.Embed(
        title=get_msg(lang, f"match_public_{state}_title"),
        description="\n".join(lines),
        color=discord.Color.green() if state == "live" else discord.Color.dark_grey(),
    )


async def edit_public_match(message, lang: str, challenger, opponent, wager: int, *,
                            state: str, url: str = "", detail: str = "") -> None:
    """공개매치 메시지를 새 상태로 바꾼다. 버튼은 떼어낸다 (누를 수 있는 시점이 지났으므로)."""
    if message is None:
        return
    try:
        await message.edit(
            embed=public_match_embed(lang, challenger, opponent, wager,
                                     state=state, url=url, detail=detail),
            view=None)
    except discord.HTTPException:
        pass


class MatchInviteView(discord.ui.View):
    """도전장 DM에 붙는 수락/거부 버튼."""

    def __init__(self, match_id: int, challenger: discord.User, opponent: discord.User,
                 wager: int, lang: str):
        super().__init__(timeout=config["pvp_config"]["invite_timeout_seconds"])
        self.match_id, self.challenger, self.opponent = match_id, challenger, opponent
        self.wager, self.lang = wager, lang
        self.answered = False
        # 공개 대결이면 start_match가 공개매치 채널에 올린 메시지를 여기에 넣어준다.
        # 비공개면 None이고 도전장은 상대 DM으로만 간다.
        self.public_message = None

        accept = discord.ui.Button(label=get_msg(lang, "match_accept"), style=discord.ButtonStyle.success)
        decline = discord.ui.Button(label=get_msg(lang, "match_decline"), style=discord.ButtonStyle.secondary)
        accept.callback, decline.callback = self.on_accept, self.on_decline
        self.add_item(accept)
        self.add_item(decline)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """도전받은 본인만 수락/거부할 수 있다.

        **공개 채널에 도전장을 뿌리면 이 검사가 없을 때 아무나 누를 수 있다** — 남의 이름으로
        수락되면서 그 사람 포인트가 판돈으로 묶인다. DM으로만 보내던 시절엔 메시지 자체가
        본인에게만 보여서 문제가 안 됐지만, 공개 옵션이 생긴 이상 반드시 필요하다.
        """
        if interaction.user.id == self.opponent.id:
            return True
        await interaction.response.send_message(
            get_msg(self.lang, "match_err_not_yours"), ephemeral=True)
        return False

    async def on_accept(self, interaction: discord.Interaction):
        self.answered = True
        self.stop()
        await interaction.response.defer()

        async with bot.pool.acquire() as conn:
            reason = await accept_match(conn, self.match_id)
            if reason:
                await interaction.edit_original_response(
                    embed=discord.Embed(description=get_msg(self.lang, f"match_err_{reason}",
                                                            opponent=self.challenger.display_name)),
                    view=None)
                return
            decks = {
                user.id: await fetch_battle_decks(conn, user.id, self.lang)
                for user in (self.challenger, self.opponent)
            }

        challenger_side = PvpSide(self.challenger, decks[self.challenger.id])
        opponent_side = PvpSide(self.opponent, decks[self.opponent.id])
        match = PvpMatch(self.match_id, challenger_side, opponent_side, self.wager, self.lang)
        match.public_message = self.public_message
        live_matches[self.match_id] = match   # 웹이 match_id로 찾아올 수 있게 등록

        # 웹 화면 링크. 참가자에게는 **토큰이 붙은 링크**(카드 제출 가능)를 DM으로 주고,
        # 공개 대결이면 채널에 **토큰 없는 링크**(읽기 전용 관전)를 한 번 더 뿌린다.
        base = web_base_url()
        for side in (challenger_side, opponent_side):
            token = issue_match_token(self.match_id, side.user.id)
            try:
                await side.user.send(get_msg(
                    self.lang, "match_link_player",
                    url=f"{base}/match?id={self.match_id}&token={token}"))
            except discord.HTTPException:
                pass   # DM이 막혀도 디스코드 선택 메뉴로 진행할 수 있으므로 매치는 계속한다

        if self.public_message is not None:
            # 공개 대결은 도전장을 지우지 않고 **그 메시지를 '진행 중'으로 고쳐 쓴다** —
            # 관전 링크를 새 메시지로 또 보내면 매치 하나에 메시지가 여러 개 쌓인다.
            await edit_public_match(self.public_message, self.lang, self.challenger,
                                    self.opponent, self.wager, state="live",
                                    url=f"{base}/match?id={self.match_id}")
        else:
            # 비공개 도전장은 수락한 뒤 할 일이 없다(버튼도 죽었고 진행은 웹에서 한다).
            # 그대로 두면 DM에 쓸모없는 메시지가 쌓이므로 지운다 — 링크는 위에서 따로 보냈다.
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass

        # 덱 선택도 웹에서 한다. 제한 시간 안에 양쪽이 다 고르지 않으면 매치를 취소하고
        # 판돈을 돌려준다 (예전에는 DeckPickView의 timeout이 이 역할을 했다).
        for side in (challenger_side, opponent_side):
            active_matches[side.user.id] = match
        match.arm(config["pvp_config"]["pick_timeout_seconds"], lambda: deck_timed_out(match))
        await match.broadcast()

    async def on_decline(self, interaction: discord.Interaction):
        self.answered = True
        self.stop()
        async with bot.pool.acquire() as conn:
            await cancel_match(conn, self.match_id)

        if self.public_message is not None:
            await interaction.response.defer()
            await edit_public_match(self.public_message, self.lang, self.challenger,
                                    self.opponent, self.wager, state="ended",
                                    detail=get_msg(self.lang, "match_public_declined"))
        else:
            await interaction.response.edit_message(
                embed=discord.Embed(description=get_msg(self.lang, "match_declined_ok")), view=None)
        try:
            await self.challenger.send(get_msg(self.lang, "match_declined_by_opponent",
                                               opponent=self.opponent.display_name))
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self.answered:
            return
        async with bot.pool.acquire() as conn:
            await cancel_match(conn, self.match_id)
        # 공개 도전장은 채널에 남아 있으므로 '무산됨'으로 바꿔준다. 안 그러면 죽은 버튼이
        # 계속 보이고, 누르면 "상호작용 실패"만 뜬다.
        await edit_public_match(self.public_message, self.lang, self.challenger,
                                self.opponent, self.wager, state="ended",
                                detail=get_msg(self.lang, "match_public_expired"))

async def start_match(interaction: discord.Interaction, opponent: discord.Member,
                      wager: int | None, public: bool = False) -> None:
    """도전장을 만들어 보낸다. `wager=None`이면 친선전(판돈 없음).

    `public=False`면 상대 DM으로만, `public=True`면 같은 카테고리의 공개매치 채널로 간다.
    판돈은 모달에서 숫자 한 칸으로 받고 **0이 곧 친선전**이라 예전처럼 명령어를 둘로
    나눌 필요가 없다 (슬래시 명령은 모드에 따라 판돈 칸을 숨길 수 없어서 나눠야 했다).
    """
    await interaction.response.defer(ephemeral=True)
    lang = resolve_lang(interaction)
    pvp_conf = config["pvp_config"]

    async def fail(key: str, **kwargs):
        await interaction.followup.send(get_msg(lang, key, **kwargs), ephemeral=True)

    if opponent.bot:
        return await fail("match_err_bot")
    if opponent.id == interaction.user.id:
        return await fail("match_err_self_challenge")
    # 판돈전은 입력이 필수라 0이나 음수가 들어올 수 있다 (친선전은 None으로 들어와 이 검사를 건너뜀)
    if wager is not None and wager < pvp_conf["min_wager"]:
        return await fail("match_err_invalid_wager", min=pvp_conf["min_wager"])
    wager = wager or 0

    try:
        async with bot.pool.acquire() as conn:
            deck_size = config["deck_config"]["deck_size"]
            if not await fetch_battle_decks(conn, interaction.user.id, lang):
                return await fail("match_err_no_deck", size=deck_size)
            if not await fetch_battle_decks(conn, opponent.id, lang):
                return await fail("match_err_opponent_no_deck", opponent=opponent.display_name)

            match_id, reason = await create_match(conn, interaction.user.id, opponent.id, wager)
            if reason:
                return await fail(f"match_err_{reason}", opponent=opponent.display_name)
    except Exception as e:
        print(f"⚠️ 매치 생성 중 DB 오류: {e}")
        return await fail("match_err_generic")

    invite = get_msg(lang, "match_invite_wager", challenger=interaction.user.display_name, wager=wager) \
        if wager else get_msg(lang, "match_invite_friendly", challenger=interaction.user.display_name)
    embed = discord.Embed(
        title=get_msg(lang, "match_invite_title"),
        # 수락 제한 시간도 카드 선택과 같은 방식으로 남은 초를 보여준다 (보내기 직전에 기한을 계산)
        description=invite + "\n" + timer_line(lang, pvp_conf["invite_timeout_seconds"]),
        color=discord.Color.orange(),
    )
    if wager:
        embed.set_footer(text=get_msg(lang, "match_invite_footer", wager=wager))

    # 공개 대결이면 **패널 채널이 아니라 같은 카테고리의 공개매치 채널**에 도전장을 올린다.
    # 패널 채널에 도전장과 관전 링크가 쌓이면 버튼 패널이 위로 밀려 올라가 기능을 쓰기 어려워진다.
    # 공개 채널에서는 아무나 버튼을 누를 수 있으므로 MatchInviteView.interaction_check가
    # 도전받은 본인인지 반드시 확인한다 — 없으면 남의 판돈이 묶인다.
    view = MatchInviteView(match_id, interaction.user, opponent, wager, lang)
    channel = None

    async def abort(key: str, **kwargs):
        """도전장을 띄우지 못하면 매치를 취소해야 한다 — 안 그러면 판돈이 묶인 채 남는다."""
        async with bot.pool.acquire() as conn:
            await cancel_match(conn, match_id)
        return await fail(key, **kwargs)

    if public:
        channel = public_match_channel(interaction.channel)
        if channel is None:
            return await abort("match_err_no_match_channel",
                               channel=public_match_channel_name(interaction.channel) or "?")
        embed.description = (f"{opponent.mention}\n" + embed.description)
        try:
            view.public_message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await abort("match_err_channel")
    else:
        try:
            await opponent.send(embed=embed, view=view)
        except discord.Forbidden:
            return await abort("match_err_dm_opponent", opponent=opponent.display_name)

    await interaction.followup.send(
        get_msg(lang, "match_sent_public" if public else "match_sent",
                opponent=opponent.display_name,
                channel=channel.mention if channel else ""), ephemeral=True)


# ---------------------------------------------------------------------------
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
        for key, style, handler in (
            ("panel_checkin", discord.ButtonStyle.success, self.on_checkin),
            ("panel_summon", discord.ButtonStyle.primary, self.on_summon),
            ("panel_summon_multi", discord.ButtonStyle.primary, self.on_summon_multi),
            ("panel_deck", discord.ButtonStyle.secondary, self.on_deck),
            ("panel_match", discord.ButtonStyle.danger, self.on_match),
            ("panel_points", discord.ButtonStyle.secondary, self.on_points),
        ):
            label = get_msg(lang, key, count=config["summon_config"]["multi_count"])                 if key == "panel_summon_multi" else get_msg(lang, key)
            button = discord.ui.Button(label=label, style=style,
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

    async def on_match(self, interaction: discord.Interaction):
        # 상대를 고르는 화면을 먼저 띄운다. 판돈은 상대를 고른 뒤 모달로 받는다 —
        # 모달은 인터랙션의 **첫 응답**으로만 열 수 있어서 버튼 -> 모달 -> 상대선택 순서가 불가능하다.
        lang = resolve_lang(interaction)
        await interaction.response.send_message(
            get_msg(lang, "panel_match_pick_opponent"),
            view=MatchOpponentView(lang), ephemeral=True)


class MatchOpponentView(discord.ui.View):
    """대결 상대를 고르는 ephemeral 화면 (고른 뒤 판돈 모달이 열린다)."""

    def __init__(self, lang: str):
        super().__init__(timeout=120)
        self.lang = lang
        self.select = discord.ui.UserSelect(placeholder=get_msg(lang, "panel_match_pick_opponent"),
                                            min_values=1, max_values=1)
        self.select.callback = self.on_pick
        self.add_item(self.select)

    async def on_pick(self, interaction: discord.Interaction):
        # **`guild.get_member()`로 상대를 찾지 말 것.** members 인텐트는 특권 인텐트라 꺼져 있고
        # (`Intents.default()`), 그러면 멤버 캐시에는 음성 채널에 들어온 사람만 들어온다
        # (`MemberCacheFlags.from_intents` -> joined=False/voice=True). 그래서 상대를 골라도
        # 대부분 None이 나와 "An error occurred"만 뜨고 대결이 시작되지 않았다.
        # `UserSelect.values`는 인터랙션 payload의 resolved에서 바로 만들어져 캐시가 필요 없고,
        # 길드 안에서는 항상 Member로 온다 (discord.py 2.7.1 소스로 확인).
        opponent = self.select.values[0]
        # 상대 선택은 새 인터랙션이라 여기서 모달을 여는 건 첫 응답 -> 허용된다
        await interaction.response.send_modal(MatchWagerModal(self.lang, opponent))


class MatchWagerModal(discord.ui.Modal):
    """판돈 입력. 0을 넣으면 친선전(포인트 이동 없음)이 된다."""

    def __init__(self, lang: str, opponent: discord.Member):
        super().__init__(title=get_msg(lang, "panel_match_modal_title"))
        self.lang, self.opponent = lang, opponent
        self.wager = discord.ui.TextInput(
            label=get_msg(lang, "panel_match_wager_label"),
            placeholder=get_msg(lang, "panel_match_wager_hint"),
            default="0", required=True, max_length=9)
        self.add_item(self.wager)
        self.public = discord.ui.TextInput(
            label=get_msg(lang, "panel_match_public_label"),
            placeholder=get_msg(lang, "panel_match_public_hint"),
            default="N", required=False, max_length=4)
        self.add_item(self.public)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.wager.value.strip().replace(",", "")
        if not raw.isdigit():
            return await interaction.response.send_message(
                get_msg(self.lang, "panel_match_wager_invalid"), ephemeral=True)
        wager = int(raw)
        public = self.public.value.strip().upper() in ("Y", "YES", "O", "공개", "T", "TRUE")
        # 판돈 0은 친선전과 같은 뜻이다 — start_match는 None을 친선전으로 보므로 변환해서 넘긴다
        await start_match(interaction, self.opponent, wager or None, public)


async def close_stale_public_matches(channel, lang: str, limit: int = 50) -> int:
    """봇이 켜질 때, 채널에 남아 있는 옛 공개 매치 메시지를 '종료'로 닫는다.

    매치 진행 상태는 메모리에만 있어서 재시작하면 사라진다 (판돈은 refund_stale_matches가
    반환한다). 그런데 **채널 메시지는 그대로 남아** 계속 '진행 중'으로 보이고, 이미 죽은 관전
    링크와 눌러도 "상호작용 실패"만 뜨는 수락 버튼이 남는다. Render 무료 플랜은 스핀다운으로
    재시작이 잦아서 이 정리가 없으면 채널이 유령 매치로 뒤덮인다.
    """
    stale = {get_msg(lang, "match_invite_title"), get_msg(lang, "match_public_live_title")}
    closed = 0
    async for message in channel.history(limit=limit):
        if message.author != bot.user or not message.embeds:
            continue
        embed = message.embeds[0]
        if embed.title not in stale:
            continue
        # 대전 링크(죽었다)·남은 시간 표시(지났다)·멘션은 빼고, 누가 붙었는지만 남긴다
        lines = [line for line in (embed.description or "").split("\n")
                 if line and "/match?id=" not in line and "<t:" not in line
                 and not line.startswith("<@")]
        lines.append(get_msg(lang, "match_public_restarted"))
        try:
            await message.edit(
                embed=discord.Embed(title=get_msg(lang, "match_public_ended_title"),
                                    description="\n".join(lines),
                                    color=discord.Color.dark_grey()),
                view=None)
            closed += 1
        except discord.HTTPException:
            pass
    return closed


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

        # 공개 매치 채널에 남은 옛 매치도 같이 닫아준다 (재시작 때 메모리 상태가 날아갔으므로)
        match_channel = discord.utils.get(category.text_channels,
                                          name=conf.get("match_channel", ""))
        if match_channel is not None:
            try:
                stale = await close_stale_public_matches(match_channel, lang)
                if stale:
                    print(f"🧹 재시작 전 공개 매치 {stale}건을 종료로 정리 ({match_channel.name})")
            except discord.HTTPException as e:
                print(f"⚠️ 공개 매치 정리 실패 ({guild.name} / {match_channel.name}): {e}")
    return posted


def main():
    token = os.environ["BOT_TOKEN"]
    bot.run(token)

if __name__ == "__main__":
    main()
