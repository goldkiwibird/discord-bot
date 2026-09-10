import asyncio
import json
import os
import random
import secrets
import sys
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import asyncpg
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from card_renderer import CardData, CardRenderer, to_png_bytes

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

# 명령어 이름/설명을 언어별로 다르게 보여주기 위한 번역 테이블을 오락실.json의 commands 섹션에서 구성.
# 키는 (기본 한국어 문자열, discord.Locale), 값은 그 로케일에서 보여줄 번역문.
COMMAND_TRANSLATIONS = {}
for _base_name, _cmd_conf in config.get("commands", {}).items():
    for _locale_value, _translated_name in _cmd_conf.get("name_localizations", {}).items():
        COMMAND_TRANSLATIONS[(_base_name, discord.Locale(_locale_value))] = _translated_name
    for _locale_value, _translated_desc in _cmd_conf.get("description_localizations", {}).items():
        COMMAND_TRANSLATIONS[(_cmd_conf["description"], discord.Locale(_locale_value))] = _translated_desc

class CommandTranslator(app_commands.Translator):
    async def translate(self, string: app_commands.locale_str, locale: discord.Locale, context: app_commands.TranslationContext) -> str | None:
        return COMMAND_TRANSLATIONS.get((string.message, locale))

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
            UNIQUE (user_id, hero_id)
        )
    """)
    # PVP는 대결마다 어느 덱을 쓸지 그때그때 고르는 방식으로 가기로 해서
    # "현재 사용 중인 덱"이라는 고정 개념 자체가 필요 없어졌다 — 예전에 추가했던 컬럼 정리.
    await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS active_deck")

    # PVP 매치. 진행 중인 판의 세부 상태(고른 카드 등)는 메모리에 두고, 여기에는
    # "판돈을 누구에게서 얼마나 미리 빼뒀는지"만 남긴다. 봇이 매치 도중에 재시작되면
    # 이 기록을 보고 묶여 있던 판돈을 돌려줘야 하기 때문 (그게 없으면 포인트가 증발한다).
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
        self.renderer = CardRenderer(config["card_config"])
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
        await self.tree.set_translator(CommandTranslator())

        dev_guild_id = os.getenv("DEV_GUILD_ID")
        if dev_guild_id:
            # 테스트 서버 하나에만 즉시 동기화 (개발 중엔 이쪽이 훨씬 빠름)
            guild = discord.Object(id=int(dev_guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            # 길드 단위가 아닌 전역 동기화라 디스코드 클라이언트에 반영되는 데 최대 1시간 정도 걸릴 수 있음
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

@bot.tree.command(
    name=app_commands.locale_str("check-in"),
    description=app_commands.locale_str(config["commands"]["check-in"]["description"]),
)
async def checkin(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    lang = resolve_lang(interaction)
    daily_points = config.get("checkin_config", {}).get("daily_points", 100)
    today = today_kst()
    yesterday = today - timedelta(days=1)

    try:
        async with bot.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (user_id, points, last_checkin_date, checkin_streak)
                VALUES ($1, $2, $3, 1)
                ON CONFLICT (user_id) DO UPDATE SET
                    points = users.points + EXCLUDED.points,
                    last_checkin_date = EXCLUDED.last_checkin_date,
                    checkin_streak = CASE
                        WHEN users.last_checkin_date = $4 THEN users.checkin_streak + 1
                        ELSE 1
                    END
                WHERE users.last_checkin_date IS DISTINCT FROM EXCLUDED.last_checkin_date
                RETURNING points, checkin_streak
                """,
                interaction.user.id, daily_points, today, yesterday
            )
    except Exception as e:
        print(f"⚠️ 출석 처리 중 DB 오류: {e}")
        await interaction.followup.send(get_msg(lang, "checkin_error"), ephemeral=True)
        return

    if row is None:
        await interaction.followup.send(get_msg(lang, "checkin_already_done"), ephemeral=True)
        return

    embed = discord.Embed(
        title=get_msg(lang, "checkin_success_title"),
        description=get_msg(lang, "checkin_success_desc", points=daily_points),
        color=discord.Color.green()
    )
    embed.add_field(name=get_msg(lang, "checkin_points_label"), value=f"{row['points']:,}P", inline=True)
    embed.add_field(name=get_msg(lang, "checkin_streak_label"), value=get_msg(lang, "checkin_streak_value", streak=row['checkin_streak']), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(
    name=app_commands.locale_str("points"),
    description=app_commands.locale_str(config["commands"]["points"]["description"]),
)
async def check_points(interaction: discord.Interaction):
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

            points = spent["points"]
            if refund:
                points = await conn.fetchval(
                    "UPDATE users SET points = points + $2 WHERE user_id = $1 RETURNING points",
                    user_id, refund,
                )

    return outcomes, owned, points

def build_cards(outcomes: list[dict], lang: str) -> list[CardData]:
    """소환 결과를 카드 이미지용 데이터로 변환.

    소환 화면에서는 항상 직업/종족 기본 스탯만 보여준다 (강화 반영 X).
    강화가 반영된 "현재 보유 스탯"은 나중에 PVP 등에서 CardData.bonus를 채워 표시한다
    (card_renderer는 이미 그 경우 "10 (+3)" 형식으로 그릴 수 있음).

    name(자산 조회 키, hero-cards/hero/*.png 파일명)은 항상 한글 원본 그대로 두고,
    화면에 그려질 display_name만 명령어를 실행한 채널 언어에 맞춰 바꾼다.
    """
    return [
        CardData(
            hero_id=outcome["hero"]["id"],
            name=outcome["hero"]["name"],
            display_name=hero_display_name(outcome["hero"], lang),
            grade=outcome["hero"]["grade"],
            job=outcome["hero"]["job"],
            element=outcome["hero"]["element"],
            stats={key: outcome["hero"][key] for key in STAT_KEYS},
        )
        for outcome in outcomes
    ]

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

def element_bonus(mine: str, theirs: str) -> int:
    """속성 상성 가산점. 나무>땅>구름>불>나무 순환(+4), 빛>어둠(+4),
    4원소가 빛을 상대할 때(+1), 어둠이 4원소를 상대할 때(+1). 그 외(마주보는 4원소 등)는 0."""
    conf = config["pvp_config"]
    cycle = conf["element_cycle"]
    basics = set(cycle)

    if cycle.get(mine) == theirs:
        return conf["element_cycle_bonus"]
    if mine == "light" and theirs == "dark":
        return conf["light_over_dark_bonus"]
    if mine in basics and theirs == "light":
        return conf["basic_over_light_bonus"]
    if mine == "dark" and theirs in basics:
        return conf["dark_over_basic_bonus"]
    return 0

def battle_stat_value(me: dict, opp: dict) -> int:
    """상성표에 따라 이 카드가 이번 대결에서 내밀 수치.

    각 진영이 (내 직업, 상대 직업)으로 자기 칸을 조회하는 구조라 공격/방어 구분이 필요 없다.
    사제전은 예외적으로 스탯 카테고리가 상대에 따라 정해진다.
    """
    rule = config["pvp_config"]["job_matchup"][me["job"]][opp["job"]]

    if rule == "OWN_LOWEST":  # 사제를 상대하는 쪽은 자기 최저 스탯을 쓴다
        return min(me[key] for key in STAT_KEYS)
    if rule == "BOTH_HIGHEST":  # 사제 vs 사제는 각자 자기 최고 스탯
        return max(me[key] for key in STAT_KEYS)
    if rule == "MATCH_OPP_LOWEST":
        # 사제는 상대의 최저 스탯과 같은 카테고리를 쓴다.
        # 최저가 여러 개면 그중 사제 본인 수치가 가장 높은 것을 고른다 (상대 수치는 어느 쪽이든 동일).
        lowest = min(opp[key] for key in STAT_KEYS)
        return max(me[key] for key in STAT_KEYS if opp[key] == lowest)
    return me[rule]

def resolve_battle(card_a: dict, card_b: dict) -> dict:
    """카드 1:1 대결. 동점은 무승부(양쪽 다 점수 없음)."""
    value_a = battle_stat_value(card_a, card_b) + element_bonus(card_a["element"], card_b["element"])
    value_b = battle_stat_value(card_b, card_a) + element_bonus(card_b["element"], card_a["element"])
    return {
        "value_a": value_a,
        "value_b": value_b,
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
    (덱 간 카드 중복은 user_decks의 UNIQUE 제약이 최종적으로 막지만, 사용자에게 이유를 알려주려면 여기서도 확인)
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
               d.deck_number
        FROM user_cards c
        JOIN hero_base_stats h ON h.id = c.hero_id
        LEFT JOIN user_decks d ON d.user_id = c.user_id AND d.hero_id = c.hero_id
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
            "deck_number": row["deck_number"],
        })

    return {"cards": cards}

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

        # 다른 덱에 이미 들어간 카드인지 확인 (같은 카드는 덱 하나에만 배치 가능)
        conflict = await conn.fetchval(
            "SELECT deck_number FROM user_decks WHERE user_id = $1 AND hero_id = ANY($2::int[]) AND deck_number <> $3",
            user_id, hero_ids, deck_number,
        )
        if conflict is not None:
            return "in_other_deck"

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

    cards = build_cards(outcomes, lang)
    # 이미지 합성은 CPU 작업이라 별도 스레드에서 돌린다 (봇 하트비트가 밀리지 않도록)
    if len(cards) == 1:
        image = await asyncio.to_thread(bot.renderer.render_card, cards[0])
    else:
        image = await asyncio.to_thread(bot.renderer.render_grid, cards)

    embed = discord.Embed(
        title=get_msg(lang, "summon_result_title"),
        description=describe_outcomes(outcomes, lang) + "\n\n" + get_msg(lang, "summon_points_left", points=f"{points:,}"),
        color=discord.Color.gold(),
    )
    embed.set_image(url="attachment://summon.png")

    # 소환 결과는 DM이 아니라 명령어를 실행한 채널에 본인에게만 보이는(ephemeral) 메시지로 전달
    await interaction.followup.send(
        embed=embed, file=discord.File(to_png_bytes(image), filename="summon.png"), ephemeral=True
    )

@bot.tree.command(
    name=app_commands.locale_str("summon"),
    description=app_commands.locale_str(config["commands"]["summon"]["description"]),
)
async def summon(interaction: discord.Interaction):
    await summon_and_reply(interaction, 1, config["summon_config"]["cost_single"])

@bot.tree.command(
    name=app_commands.locale_str("summon-10"),
    description=app_commands.locale_str(config["commands"]["summon-10"]["description"]),
)
async def summon_ten(interaction: discord.Interaction):
    summon_conf = config["summon_config"]
    await summon_and_reply(interaction, summon_conf["multi_count"], summon_conf["cost_multi"])

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
                    "web_reset_enhance_failed")
    }
    data["stat_labels"] = {key: get_msg(lang, f"stat_{key}") for key in STAT_KEYS}
    data["job_labels"] = {key: get_msg(lang, f"job_{key}") for key in data["jobs"]}
    data["element_labels"] = {key: get_msg(lang, f"element_{key}") for key in data["elements"]}
    return web.json_response(data)

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

async def start_web_server():
    """Render는 웹 서비스가 PORT를 열고 있어야 해서, 헬스체크 겸 덱 편성 페이지를 여기서 서빙한다."""
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="Arcade bot is online!"))
    app.router.add_get("/deck", handle_deck_page)
    app.router.add_get("/api/deck", handle_deck_data)
    app.router.add_post("/api/deck", handle_deck_save)
    app.router.add_post("/api/deck/reset-enhance", handle_deck_reset_enhance)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"🌐 웹서버 구동 완료 (포트: {port})")

def web_base_url() -> str:
    """덱 링크에 쓸 공개 URL. Render가 자동으로 넣어주는 값을 우선 쓰고, 없으면 설정/로컬 순."""
    return (config["web_config"]["base_url"]
            or os.getenv("RENDER_EXTERNAL_URL")
            or f"http://localhost:{os.environ.get('PORT', 10000)}").rstrip("/")

@bot.tree.command(
    name=app_commands.locale_str("deck"),
    description=app_commands.locale_str(config["commands"]["deck"]["description"]),
)
async def deck(interaction: discord.Interaction):
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

    def other(self, user_id: int) -> PvpSide:
        return self.opponent if user_id == self.challenger.user.id else self.challenger

active_matches: dict[int, PvpMatch] = {}  # user_id -> 참여 중인 매치

def to_card_data(card: dict) -> CardData:
    """fetch_battle_decks()가 내려주는 카드 dict를 카드 렌더러 입력으로 변환.

    보유 카드 화면이므로 강화가 반영된 현재 스탯 + 증가분을 그대로 넘긴다("10 (+3)" 표기).
    """
    return CardData(
        hero_id=card["hero_id"], name=card["name"], grade=card["grade"], job=card["job"],
        element=card["element"],
        stats={key: card[key] for key in STAT_KEYS}, bonus=card["bonus"], display_name=card["display_name"],
    )

def timer_line(lang: str, seconds: int) -> str:
    """남은 제한 시간을 디스코드 상대 타임스탬프(`<t:...:R>`)로 표시하는 한 줄.

    봇이 매초 메시지를 고쳐 쓰는 대신 디스코드 클라이언트가 알아서 카운트다운해주므로
    API 호출이나 편집 레이트리밋 부담이 전혀 없다 (대신 초 단위가 딱 맞지는 않는다).
    View의 타임아웃은 메시지가 전송된 시점부터 재므로, 이 줄은 메시지를 보내기 직전에 만들어야 한다.
    """
    return get_msg(lang, "match_time_left", time=f"<t:{int(time.time()) + seconds}:R>")

async def push_match_view(side: PvpSide, embed: discord.Embed, view: discord.ui.View | None) -> None:
    """참가자의 DM 화면을 제자리에서 갱신 (없으면 새로 보냄)."""
    if side.message is None:
        side.message = await side.user.send(embed=embed, view=view)
    else:
        await side.message.edit(embed=embed, view=view)

async def resend_match_view(side: PvpSide, embed: discord.Embed, view: discord.ui.View | None) -> None:
    """참가자의 DM 화면을 지우고 새 메시지로 다시 보낸다.

    상대 덱 공개나 라운드 판정 결과는 이미지 첨부 때문에 매번 새 메시지로 나가는데, 선택 패널을
    제자리에서 고쳐 쓰면 그 이미지들 위로 밀려 올라가서 유저가 스크롤을 올려야 보인다 —
    제한 시간이 있는 화면이니 항상 DM 최하단에 오도록 다시 보낸다.
    새로 보내는 게 실패하면 기존 메시지라도 남도록 전송을 먼저 하고 나중에 지운다.
    """
    old = side.message
    side.message = await side.user.send(embed=embed, view=view)
    if old is not None:
        try:
            await old.delete()
        except discord.HTTPException:
            pass

async def finish_match(match: PvpMatch, winner_id: int | None, reason_key: str | None = None) -> None:
    """정산하고 양쪽에 최종 결과를 보여준 뒤 매치를 정리한다."""
    if match.finished:
        return
    match.finished = True

    try:
        async with bot.pool.acquire() as conn:
            if reason_key:  # 시간 초과 등으로 중단된 경우
                await cancel_match(conn, match.match_id)
            else:
                await settle_match(conn, match.match_id, winner_id)
    except Exception as e:
        print(f"⚠️ 매치 정산 중 DB 오류: {e}")

    for side in (match.challenger, match.opponent):
        active_matches.pop(side.user.id, None)
        them = match.other(side.user.id)
        lang = match.lang

        if reason_key:
            embed = discord.Embed(description=get_msg(lang, reason_key), color=discord.Color.greyple())
        else:
            if winner_id is None:
                title, reward = "match_final_void", ("match_reward_void", match.wager)
            elif winner_id == side.user.id:
                title, reward = "match_final_win", ("match_reward_win", match.wager)
            else:
                title, reward = "match_final_lose", ("match_reward_lose", match.wager)

            description = get_msg(lang, title, you=side.wins, them=them.wins)
            if match.wager:
                description += "\n" + get_msg(lang, reward[0], amount=reward[1])
            embed = discord.Embed(
                description=description,
                color=discord.Color.gold() if winner_id == side.user.id else discord.Color.greyple(),
            )

        # 승패 결과는 DM으로 보낸다. 인터랙션에 붙는 ephemeral 응답은 쓸 수 없다 —
        # 인터랙션은 3초 안에 응답해야 하는데 최종 결과는 상대가 카드를 고를 때까지(최대 15초)
        # 기다려야 나오므로, 그 시점엔 이미 데드라인이 지나 있다. 모달도 같은 이유로 불가능.
        try:
            # 마지막 판정 이미지 아래에 결과가 오도록, 남아 있던 선택 패널은 지우고 새로 보낸다
            await resend_match_view(side, embed, None)
        except discord.HTTPException:
            pass

async def start_round(match: PvpMatch) -> None:
    """다음 라운드 시작. 남은 카드가 1장뿐이면 고를 것도 없으니 자동으로 낸다."""
    match.round_no += 1
    deck_size = config["deck_config"]["deck_size"]

    for side in (match.challenger, match.opponent):
        side.pick = None

    auto = [side for side in (match.challenger, match.opponent) if len(side.remaining) == 1]
    for side in auto:
        side.pick = side.remaining[0]

    if len(auto) == 2:  # 양쪽 다 마지막 카드 -> 바로 판정
        await resolve_round(match)
        return

    round_timeout = config["pvp_config"]["round_pick_timeout_seconds"]
    for side in (match.challenger, match.opponent):
        them = match.other(side.user.id)
        waiting = side.pick is not None  # 마지막 남은 카드라 자동 선택된 쪽은 고를 게 없다
        description = get_msg(match.lang, "match_waiting_pick") if waiting else (
            get_msg(match.lang, "match_round_prompt", round=match.round_no, total=deck_size)
            + "\n" + timer_line(match.lang, round_timeout))
        embed = discord.Embed(
            title=get_msg(match.lang, "match_vs",
                          challenger=match.challenger.user.display_name,
                          opponent=match.opponent.user.display_name),
            description=description,
            color=discord.Color.blurple(),
        )
        embed.add_field(name=get_msg(match.lang, "match_score",
                                     you=side.wins, them=them.wins), value="​", inline=False)

        await resend_match_view(side, embed, None if waiting else CardPickView(match, side))

async def resolve_round(match: PvpMatch) -> None:
    """양쪽 카드가 다 나왔을 때 승패를 계산하고 다음 라운드로 넘어간다."""
    a, b = match.challenger, match.opponent
    result = resolve_battle(a.pick, b.pick)
    if result["winner"] == "a":
        a.wins += 1
    elif result["winner"] == "b":
        b.wins += 1

    for side in (a, b):
        side.remaining = [c for c in side.remaining if c["hero_id"] != side.pick["hero_id"]]

    for side, value_key, other_key in ((a, "value_a", "value_b"), (b, "value_b", "value_a")):
        them = match.other(side.user.id)
        if result["winner"] is None:
            key = "match_round_result_draw"
        elif (result["winner"] == "a") == (side is a):
            key = "match_round_result_win"
        else:
            key = "match_round_result_lose"

        embed = discord.Embed(
            description=get_msg(match.lang, key, round=match.round_no,
                                you=side.pick["display_name"], them=them.pick["display_name"],
                                you_value=result[value_key], them_value=result[other_key]),
            color=discord.Color.green() if key.endswith("win")
            else discord.Color.red() if key.endswith("lose") else discord.Color.greyple(),
        )
        embed.add_field(name=get_msg(match.lang, "match_score", you=side.wins, them=them.wins),
                        value="​", inline=False)

        # 내 카드 vs 상대 카드를 나란히 놓고 그 사이에 이번 판정에 쓰인 스탯값을 이미지로 보여준다
        image = await asyncio.to_thread(
            bot.renderer.render_matchup,
            to_card_data(side.pick), result[value_key],
            to_card_data(them.pick), result[other_key],
        )
        embed.set_image(url="attachment://matchup.png")
        try:
            await side.user.send(embed=embed, file=discord.File(to_png_bytes(image), filename="matchup.png"))
        except discord.HTTPException:
            pass

    if a.remaining:
        await start_round(match)
    else:
        winner_id = (a.user.id if a.wins > b.wins
                     else b.user.id if b.wins > a.wins else None)
        await finish_match(match, winner_id)

class CardPickView(discord.ui.View):
    """이번 라운드에 낼 카드를 고르는 선택 메뉴 (남은 카드는 최대 5장이라 한 메뉴에 다 들어간다)."""

    def __init__(self, match: PvpMatch, side: PvpSide):
        super().__init__(timeout=config["pvp_config"]["round_pick_timeout_seconds"])
        self.match, self.side = match, side

        select = discord.ui.Select(
            placeholder=get_msg(match.lang, "match_round_prompt",
                                round=match.round_no, total=config["deck_config"]["deck_size"]),
            options=[
                discord.SelectOption(
                    label=f"{card['display_name']} ({card['grade']})",
                    description=(get_msg(match.lang, "element_" + card["element"]) + " · "
                                 + get_msg(match.lang, "job_" + card["job"])),
                    value=str(card["hero_id"]),
                )
                for card in side.remaining
            ],
        )
        select.callback = self.on_pick
        self.add_item(select)

    async def on_pick(self, interaction: discord.Interaction):
        if self.match.finished or self.side.pick is not None:
            await interaction.response.defer()
            return

        hero_id = int(interaction.data["values"][0])
        self.side.pick = next(c for c in self.side.remaining if c["hero_id"] == hero_id)
        self.stop()

        embed = discord.Embed(description=get_msg(self.match.lang, "match_waiting_pick"),
                              color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=None)

        async with self.match.lock:
            them = self.match.other(self.side.user.id)
            if them.pick is not None and not self.match.finished:
                await resolve_round(self.match)

    async def on_timeout(self):
        # 제한 시간 안에 못 고르면 남은 카드 중 하나를 무작위로 대신 낸다 (매치를 취소하지 않음).
        if self.match.finished or self.side.pick is not None:
            return
        self.side.pick = random.choice(self.side.remaining)

        embed = discord.Embed(description=get_msg(self.match.lang, "match_waiting_pick"),
                              color=discord.Color.blurple())
        try:
            await push_match_view(self.side, embed, None)
        except discord.HTTPException:
            pass

        async with self.match.lock:
            them = self.match.other(self.side.user.id)
            if them.pick is not None and not self.match.finished:
                await resolve_round(self.match)

class MatchInviteView(discord.ui.View):
    """도전장 DM에 붙는 수락/거부 버튼."""

    def __init__(self, match_id: int, challenger: discord.User, opponent: discord.User,
                 wager: int, lang: str):
        super().__init__(timeout=config["pvp_config"]["invite_timeout_seconds"])
        self.match_id, self.challenger, self.opponent = match_id, challenger, opponent
        self.wager, self.lang = wager, lang
        self.answered = False

        accept = discord.ui.Button(label=get_msg(lang, "match_accept"), style=discord.ButtonStyle.success)
        decline = discord.ui.Button(label=get_msg(lang, "match_decline"), style=discord.ButtonStyle.secondary)
        accept.callback, decline.callback = self.on_accept, self.on_decline
        self.add_item(accept)
        self.add_item(decline)

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

        # 상대(수락한 쪽)의 화면은 방금 누른 도전장 메시지를 그대로 이어서 쓴다
        opponent_side.message = await interaction.original_response()

        try:
            challenger_side.message = await self.challenger.send(
                embed=discord.Embed(description=get_msg(self.lang, "match_deck_prompt"),
                                    color=discord.Color.blurple()))
        except discord.Forbidden:
            # 도전자에게 DM을 못 보내면 진행이 불가능하므로 판돈을 되돌리고 종료
            async with bot.pool.acquire() as conn:
                await cancel_match(conn, self.match_id)
            await interaction.edit_original_response(
                embed=discord.Embed(description=get_msg(self.lang, "match_err_dm_opponent",
                                                        opponent=self.challenger.display_name)),
                view=None)
            return

        deck_timeout = config["pvp_config"]["pick_timeout_seconds"]
        for side in (challenger_side, opponent_side):
            active_matches[side.user.id] = match
            await push_match_view(
                side,
                discord.Embed(description=get_msg(self.lang, "match_deck_prompt")
                              + "\n" + timer_line(self.lang, deck_timeout),
                              color=discord.Color.blurple()),
                DeckPickView(match, side))

    async def on_decline(self, interaction: discord.Interaction):
        self.answered = True
        self.stop()
        async with bot.pool.acquire() as conn:
            await cancel_match(conn, self.match_id)

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

class DeckPickView(discord.ui.View):
    """3개 덱 중 이번 대결에 쓸 덱을 고르는 메뉴."""

    def __init__(self, match: PvpMatch, side: PvpSide):
        super().__init__(timeout=config["pvp_config"]["pick_timeout_seconds"])
        self.match, self.side = match, side

        select = discord.ui.Select(
            placeholder=get_msg(match.lang, "match_deck_prompt"),
            options=[
                discord.SelectOption(
                    label=get_msg(match.lang, "match_deck_option", number=number),
                    description=", ".join(c["display_name"] for c in cards)[:100],
                    value=str(number),
                )
                for number, cards in sorted(side.decks.items())
            ],
        )
        select.callback = self.on_pick
        self.add_item(select)

    async def on_pick(self, interaction: discord.Interaction):
        if self.side.deck_number is not None or self.match.finished:
            await interaction.response.defer()
            return

        self.side.choose_deck(int(interaction.data["values"][0]))
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description=get_msg(self.match.lang, "match_waiting_deck"),
                                color=discord.Color.blurple()),
            view=None)

        async with self.match.lock:
            them = self.match.other(self.side.user.id)
            if them.deck_number is None or self.match.finished:
                return

            # 양쪽 덱이 정해졌으니 상대방의 덱 5장만 카드 이미지 한 줄로 공개하고 1라운드 시작
            # (내 덱은 이미 알고 있으니 안 보여줘도 됨)
            for side in (self.match.challenger, self.match.opponent):
                other = self.match.other(side.user.id)
                cards = [to_card_data(card) for card in other.remaining]
                image = await asyncio.to_thread(bot.renderer.render_grid, cards)
                embed = discord.Embed(
                    title=get_msg(self.match.lang, "match_vs",
                                  challenger=self.match.challenger.user.display_name,
                                  opponent=self.match.opponent.user.display_name),
                    description=get_msg(self.match.lang, "match_opponent_deck"),
                    color=discord.Color.blurple())
                embed.set_image(url="attachment://opponent_deck.png")
                await side.user.send(embed=embed,
                                     file=discord.File(to_png_bytes(image), filename="opponent_deck.png"))

            await start_round(self.match)

    async def on_timeout(self):
        if not self.match.finished:
            await finish_match(self.match, None, reason_key="match_cancelled_timeout")

async def start_match(interaction: discord.Interaction, opponent: discord.Member,
                      wager: int | None) -> None:
    """도전장을 만들어 상대 DM으로 보낸다. `wager=None`이면 친선전(판돈 없음).

    판돈전(`/match`)과 친선전(`/friendly`)을 **별도 명령어로 나눈 이유**: 디스코드 슬래시 명령은
    다른 옵션 값에 따라 특정 옵션을 숨길 수 없다. 하나의 명령어에 모드 선택으로 합쳐두면
    친선전을 골라도 판돈 입력칸이 그대로 보여서, 판돈을 적어 넣고 "걸었다"고 오해하게 된다.
    명령어를 나누면 친선전에는 판돈 입력칸 자체가 없어 그 오해가 원천적으로 불가능해진다.
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

    try:
        await opponent.send(embed=embed,
                            view=MatchInviteView(match_id, interaction.user, opponent, wager, lang))
    except discord.Forbidden:
        async with bot.pool.acquire() as conn:
            await cancel_match(conn, match_id)
        return await fail("match_err_dm_opponent", opponent=opponent.display_name)

    await interaction.followup.send(
        get_msg(lang, "match_sent", opponent=opponent.display_name), ephemeral=True)

@bot.tree.command(
    name=app_commands.locale_str("match"),
    description=app_commands.locale_str(config["commands"]["match"]["description"]),
)
async def match(interaction: discord.Interaction, opponent: discord.Member, wager: int):
    await start_match(interaction, opponent, wager)

@bot.tree.command(
    name=app_commands.locale_str("friendly"),
    description=app_commands.locale_str(config["commands"]["friendly"]["description"]),
)
async def friendly(interaction: discord.Interaction, opponent: discord.Member):
    await start_match(interaction, opponent, None)

def main():
    token = os.environ["BOT_TOKEN"]
    bot.run(token)

if __name__ == "__main__":
    main()
