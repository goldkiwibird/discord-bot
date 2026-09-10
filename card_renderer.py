"""카드 이미지 생성 모듈.

소환 / 보유 카드 조회 / 덱 편성 / PVP 등 어느 기능에서든 쓸 수 있도록
디스코드 봇 로직과 완전히 분리해 두었다. 입력은 CardData 하나뿐이고,
출력은 PIL 이미지 또는 PNG 바이트다.

카드에서 유저마다 달라지는 부분은 하단 스탯 수치 5개뿐이라,
프레임 + 캐릭터 + 이름 + 직업 아이콘까지 합성한 "베이스 카드"를 캐릭터별로
한 번만 만들어 캐시하고, 요청 때는 그 위에 스탯만 그린다.
"""

import io
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

# 한글 캐릭터 이름을 그려야 해서 한글 글리프가 있는 폰트가 반드시 필요하다.
# 리눅스(Render)에는 기본으로 깔린 한글 폰트가 없을 수 있어서, 프로젝트 fonts/ 폴더를 가장 먼저 찾는다.
FONT_CANDIDATES = [
    "fonts/NanumGothic.ttf",
    "fonts/NotoSansKR-Regular.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


@dataclass(frozen=True)
class CardData:
    """카드 한 장을 그리는 데 필요한 최소 정보.

    name은 hero-cards/hero/{name}.png 파일명과 일치해야 하는 '자산 조회 키'다
    (이미지 파일명이 한글 원본 이름으로 되어 있어서 항상 한글이어야 함).
    화면에 실제로 그려질 이름은 display_name이며, 지정하지 않으면 name을 그대로 쓴다
    (카드 이미지에 표시되는 언어를 name과 분리해 영어/번체 등으로 바꿀 수 있게 하기 위함).

    job/element도 자산 조회 키다 (hero-cards/job_icon/{job}.png, hero-cards/element/{element}.png).
    element가 비어 있으면 속성 아이콘만 생략하고 나머지는 그대로 그린다.

    stats는 강화분이 이미 더해진 '현재 스탯'이어야 한다
    (기본 스탯이 아니라 그 유저가 실제로 보유한 카드의 스탯).
    bonus는 그중 강화로 올라간 증가분만 담는다 (표시 시 "10 (+3)"처럼 따로 보여주기 위함).
    강화 이력을 모르는 상황(예: 도감 미리보기)에서는 비워두면 그냥 최종 수치만 표시된다.
    """
    hero_id: int
    name: str
    grade: str
    job: str
    element: str = ""
    stats: dict = field(default_factory=dict)
    bonus: dict = field(default_factory=dict)
    display_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.name


class CardRenderer:
    def __init__(self, card_config: dict, base_dir: str = "."):
        self.config = card_config
        self.assets_dir = os.path.join(base_dir, card_config.get("assets_dir", "hero-cards"))
        self.base_dir = base_dir
        # 베이스 카드는 (캐릭터 × 언어) 조합마다 512x768 이미지를 통째로 들고 있어서
        # 상한 없이 두면 로스터가 늘어날수록 메모리가 무한정 커진다 (54종×3언어=243MB, 200종이면 900MB로 Render
        # 무료 인스턴스 512MB를 넘김). 그래서 개수 상한을 두고 오래 안 쓴 것부터 버리는 LRU로 관리한다.
        # render_card()가 asyncio.to_thread로 실제 OS 스레드에서 동시에 여러 개 돌아갈 수 있어서
        # (동시 소환), LRU 갱신(순서 이동+삭제)은 반드시 락으로 보호해야 캐시가 깨지지 않는다.
        self._base_card_limit = card_config.get("base_card_cache_size", 60)
        self._base_cards: OrderedDict[tuple[int, str], Image.Image] = OrderedDict()
        self._base_cards_lock = threading.Lock()
        # 에셋은 로컬 hero-cards 폴더에 있으면 그걸 쓰고, 없으면 이 주소의 공개 버킷에서 받는다
        # (Render에는 14MB짜리 일러스트를 배포하지 않고 버킷에서 받아 쓰기 위함).
        # 덱 편성 웹페이지가 브라우저에 내려주는 이미지 주소와 반드시 같은 값이어야 하므로
        # 설정 키를 따로 만들지 않고 image_base_url 하나를 서버/브라우저가 공유한다.
        self.image_base_url = (card_config.get("image_base_url") or "").rstrip("/")
        self._asset_timeout = card_config.get("asset_fetch_timeout_seconds", 10)
        # 원본 에셋 캐시. 버킷에서 받는 경우 재조회가 네트워크 왕복이라 캐시가 더 중요해진다.
        # LRU 갱신(순서 이동+삭제)이 들어가므로 베이스 카드 캐시와 같은 이유로 락이 필요하다.
        self._portrait_limit = card_config.get("portrait_cache_size", 80)
        self._images: OrderedDict[tuple[str, ...], Image.Image] = OrderedDict()
        self._images_lock = threading.Lock()
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}

    # --- 리소스 로딩 -------------------------------------------------

    def _font_path(self) -> str:
        configured = self.config.get("font_path")
        candidates = ([configured] if configured else []) + FONT_CANDIDATES
        for path in candidates:
            full = path if os.path.isabs(path) else os.path.join(self.base_dir, path)
            if os.path.exists(full):
                return full
            if os.path.exists(path):
                return path
        raise FileNotFoundError(
            "한글 폰트를 찾을 수 없습니다. fonts/NanumGothic.ttf 를 프로젝트에 추가하거나 "
            "오락실.json의 card_config.font_path에 폰트 경로를 지정하세요."
        )

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        """카드 글자는 전부 굵게 표시한다. 가변 폰트가 아니면(Bold 인스턴스가 없으면) 그냥 기본 굵기로 둔다."""
        if size not in self._fonts:
            font = ImageFont.truetype(self._font_path(), size)
            try:
                font.set_variation_by_name("Bold")
            except (OSError, AttributeError):
                pass
            self._fonts[size] = font
        return self._fonts[size]

    def _read_asset(self, parts: tuple[str, ...]) -> Image.Image:
        """에셋 원본 1개를 읽는다. 로컬 파일이 있으면 그걸 쓰고, 없으면 공개 버킷에서 받는다.

        로컬 우선인 이유: 배포 환경에는 에셋을 올리지 않고 버킷에서 받지만, 개발/테스트에서는
        hero-cards 폴더만 있으면 네트워크 없이 그대로 돌아가야 한다.
        """
        local = os.path.join(self.assets_dir, *parts)
        if os.path.exists(local):
            return Image.open(local).convert("RGBA")
        if not self.image_base_url:
            raise FileNotFoundError(
                f"에셋을 찾을 수 없습니다: {local}. 로컬에 파일을 두거나 "
                f"오락실.json의 card_config.image_base_url에 공개 버킷 주소를 지정하세요."
            )
        # 캐릭터 이미지 파일명이 한글 원본이라(hero/가루가.png) 반드시 퍼센트 인코딩해야 한다 —
        # urllib은 URL을 ascii로 인코딩하므로 한글을 그대로 넣으면 UnicodeEncodeError로 죽는다.
        url = "/".join((self.image_base_url, *(urllib.parse.quote(part) for part in parts)))
        try:
            with urllib.request.urlopen(url, timeout=self._asset_timeout) as response:
                data = response.read()
        except (urllib.error.URLError, TimeoutError) as e:
            # 네트워크는 언제든 실패할 수 있고, 여기서 안 잡으면 렌더 스레드가 그대로 죽는다
            raise FileNotFoundError(f"에셋을 받지 못했습니다: {url} ({e})") from e
        return Image.open(io.BytesIO(data)).convert("RGBA")

    def _evict_portraits(self) -> None:
        """호출하는 쪽이 _images_lock을 쥔 상태여야 한다.

        로스터 크기에 비례해 늘어나는 건 캐릭터 일러스트(hero/)뿐이라 거기에만 상한을 둔다.
        프레임/아이콘은 종류가 고정(등급 4장 + 아이콘 17장)이고 모든 카드 렌더에 매번 쓰이므로,
        같은 상한에 섞으면 캐릭터 그림에 밀려 빠졌다가 매번 다시 받는 낭비가 생긴다.
        """
        portraits = [key for key in self._images if key[0] == "hero"]
        for key in portraits[:max(0, len(portraits) - self._portrait_limit)]:
            del self._images[key]

    def _load(self, *parts: str, size: tuple[int, int] | None = None) -> Image.Image:
        """에셋을 읽어서 캐시한다. 반환된 이미지는 캐시된 원본이므로 수정하려면 copy()할 것."""
        key = parts + ((str(size),) if size else ())

        with self._images_lock:
            cached = self._images.get(key)
            if cached is not None:
                self._images.move_to_end(key)  # 방금 썼으니 LRU상 가장 최근으로
                return cached

        # 락 밖에서 읽는다 — 버킷 조회는 네트워크 대기가 있어서, 락을 쥔 채로 받으면
        # 다른 스레드의 캐시 조회까지 전부 그 시간만큼 멈춰버린다.
        image = self._read_asset(parts)
        if size:
            image = image.resize(size, Image.LANCZOS)

        with self._images_lock:
            existing = self._images.get(key)
            if existing is not None:  # 그 사이 다른 스레드가 먼저 받아 넣었으면 그걸 재사용
                self._images.move_to_end(key)
                return existing
            self._images[key] = image
            self._evict_portraits()
            return image

    # --- 베이스 카드 (캐릭터별 1회 생성 후 캐시) ----------------------

    def _build_base_card(self, card: CardData) -> Image.Image:
        canvas = self._load("card", f"{card.grade}.png").copy()

        # 캐릭터 일러스트: 비율을 유지한 채 일러스트 영역 안에 다 들어가도록 맞추고 가운데 정렬
        px0, py0, px1, py1 = self.config["portrait_box"]
        source = self._load("hero", f"{card.name}.png")
        scale = min((px1 - px0) / source.width, (py1 - py0) / source.height)
        portrait = source.resize((round(source.width * scale), round(source.height * scale)), Image.LANCZOS)
        canvas.alpha_composite(
            portrait,
            (px0 + (px1 - px0 - portrait.width) // 2, py0 + (py1 - py0 - portrait.height) // 2),
        )

        # 직업 아이콘: 프레임 우상단의 빈 원 안에 배치
        icon_size = self.config["job_icon_size"]
        job_icon = self._load("job_icon", f"{card.job}.png", size=(icon_size, icon_size))
        cx, cy = self.config["job_icon_center"]
        canvas.alpha_composite(job_icon, (cx - icon_size // 2, cy - icon_size // 2))

        # 속성 아이콘: 직업 아이콘을 카드 세로 중심선에 대해 뒤집은 자리에 같은 크기로 배치.
        # 좌표를 별도 설정값으로 두면 직업 아이콘 위치를 조정할 때 대칭이 깨지므로 항상 계산한다.
        if card.element:
            element_icon = self._load("element", f"{card.element}.png", size=(icon_size, icon_size))
            canvas.alpha_composite(element_icon, (canvas.width - cx - icon_size // 2, cy - icon_size // 2))

        # 스탯 아이콘: 유저마다 달라지는 건 옆에 적힐 숫자뿐이고 아이콘 자체는 항상 같으므로
        # (캐릭터 무관, 언어 무관) 베이스 카드에 미리 구워넣는다 — render_card()는 숫자만 그리면 됨
        stat_icon_size = self.config["stat_icon_size"]
        for stat_key, icon_x, center_y in self._stat_layout():
            icon = self._load("stat_icon", f"{stat_key}.png", size=(stat_icon_size, stat_icon_size))
            canvas.alpha_composite(icon, (icon_x, round(center_y - stat_icon_size / 2)))

        # 캐릭터 이름: 상단 패널 가운데. 이름이 길면 패널을 넘지 않도록 폰트를 줄인다.
        # (실제로 그리는 텍스트는 표시용 이름(label)이지 자산 조회 키(name)가 아님 — 언어별로 다를 수 있음)
        nx0, ny0, nx1, ny1 = self.config["name_box"]
        label = card.label
        draw = ImageDraw.Draw(canvas)
        size = self.config["name_font_size"]
        while size > 10:
            font = self._font(size)
            if draw.textlength(label, font=font) <= (nx1 - nx0) - 16:
                break
            size -= 2
        font = self._font(size)
        draw.text(((nx0 + nx1) / 2, (ny0 + ny1) / 2), label, font=font, fill=(255, 255, 255, 255), anchor="mm")

        return canvas

    def _base_card(self, card: CardData) -> Image.Image:
        # 베이스 카드에는 이름 텍스트가 그려져 들어가므로, hero_id만으로 캐싱하면
        # 먼저 렌더링된 언어가 다른 언어 요청에도 잘못 재사용된다 (label까지 키에 포함).
        key = (card.hero_id, card.label)

        with self._base_cards_lock:
            cached = self._base_cards.get(key)
            if cached is not None:
                self._base_cards.move_to_end(key)  # 방금 썼으니 LRU상 가장 최근으로
                return cached

        # 락 밖에서 실제 렌더링 (PIL 작업이라 상대적으로 오래 걸림 — 락을 오래 쥐고 있으면
        # 동시 소환 시 서로를 기다리게 되어 스레드 분산의 의미가 없어짐)
        built = self._build_base_card(card)

        with self._base_cards_lock:
            # 그 사이 다른 스레드가 같은 카드를 먼저 만들어 넣었을 수 있음 — 있으면 그걸 재사용
            existing = self._base_cards.get(key)
            if existing is not None:
                self._base_cards.move_to_end(key)
                return existing
            self._base_cards[key] = built
            if len(self._base_cards) > self._base_card_limit:
                self._base_cards.popitem(last=False)  # 가장 오래 안 쓴 것부터 제거
            return built

    def _stat_layout(self):
        """스탯 패널 안에서 스탯 5종의 아이콘 위치를 계산해 (키, 아이콘 x, 세로중심 y) 순서로 낸다.

        베이스 카드에 아이콘을 구워넣을 때와 render_card()에서 숫자를 그릴 때 둘 다 이 좌표를
        써야 아이콘과 숫자가 어긋나지 않는다 — 계산식을 한 곳에만 두는 이유.
        """
        sx0, sy0, sx1, sy1 = self.config["stat_panel"]
        order = self.config["stat_order"]
        rows = (len(order) + 1) // 2  # 참고 이미지와 같은 2열 배치 (짝수 번째는 왼쪽, 홀수 번째는 오른쪽)
        col_width = (sx1 - sx0) / 2
        row_height = (sy1 - sy0) / rows

        for index, stat_key in enumerate(order):
            col, row = index % 2, index // 2
            icon_x = round(sx0 + col_width * col + col_width * 0.22)
            center_y = sy0 + row_height * (row + 0.5)
            yield stat_key, icon_x, center_y

    # --- 최종 카드 ---------------------------------------------------

    def render_card(self, card: CardData) -> Image.Image:
        """카드 1장을 그린다. 아이콘은 이미 베이스 카드에 구워져 있으니 숫자만 매번 새로 그린다."""
        canvas = self._base_card(card).copy()
        draw = ImageDraw.Draw(canvas)

        icon_size = self.config["stat_icon_size"]
        font = self._font(self.config["stat_font_size"])

        for stat_key, icon_x, center_y in self._stat_layout():
            value = card.stats.get(stat_key, 0)
            gained = card.bonus.get(stat_key, 0)
            text = f"{value} (+{gained})" if gained else str(value)
            draw.text(
                (icon_x + icon_size + 14, center_y),
                text,
                font=font,
                fill=(255, 255, 255, 255),
                anchor="lm",
            )

        return canvas

    def render_grid(self, cards: list[CardData], columns: int | None = None) -> Image.Image:
        """여러 장을 한 장의 그리드 이미지로 합친다 (10연 소환 결과 등)."""
        columns = columns or self.config["grid_columns"]
        card_width = self.config["grid_card_width"]
        gap = 12

        thumbs = []
        for card in cards:
            full = self.render_card(card)
            scale = card_width / full.width
            thumbs.append(full.resize((card_width, round(full.height * scale)), Image.LANCZOS))

        rows = (len(thumbs) + columns - 1) // columns
        cell_h = max(t.height for t in thumbs)
        sheet = Image.new(
            "RGBA",
            (columns * card_width + (columns - 1) * gap, rows * cell_h + (rows - 1) * gap),
            (0, 0, 0, 0),
        )
        for index, thumb in enumerate(thumbs):
            col, row = index % columns, index // columns
            sheet.alpha_composite(thumb, (col * (card_width + gap), row * (cell_h + gap)))
        return sheet

    def render_matchup(self, left: CardData, left_value: int, right: CardData, right_value: int) -> Image.Image:
        """PVP 라운드 결과: 내 카드와 상대 카드를 나란히 놓고 그 사이에 판정에 쓰인 스탯값을 적는다."""
        card_width = self.config["grid_card_width"]
        font = self._font(self.config.get("matchup_value_font_size", 44))

        thumbs = []
        for card in (left, right):
            full = self.render_card(card)
            scale = card_width / full.width
            thumbs.append(full.resize((card_width, round(full.height * scale)), Image.LANCZOS))

        # "6 VS 9"를 한 줄로 적는다. 스탯이 세 자리로 커져도 글자가 잘리거나 줄바꿈되지 않도록
        # 실제 글자 폭을 재서 카드 사이 간격을 넓힌다 (matchup_gap은 최소 간격).
        text = f"{left_value} VS {right_value}"
        padding = self.config.get("matchup_text_padding", 28)
        gap = max(self.config.get("matchup_gap", 100), round(font.getlength(text)) + padding * 2)

        height = max(t.height for t in thumbs)
        sheet = Image.new("RGBA", (card_width * 2 + gap, height), (0, 0, 0, 0))
        sheet.alpha_composite(thumbs[0], (0, 0))
        sheet.alpha_composite(thumbs[1], (card_width + gap, 0))

        draw = ImageDraw.Draw(sheet)
        # 카드 사이 여백은 원래 투명이라, 디스코드가 라이트 테마면 흰 글자가 안 보인다.
        # 테마와 무관하게 읽히도록 그 자리에 어두운 배경판을 깔고 그 위에 글자를 그린다.
        draw.rectangle([card_width, 0, card_width + gap, height], fill=(27, 29, 36, 235))
        draw.text(
            (card_width + gap / 2, height / 2),
            text,
            font=font, fill=(255, 255, 255, 255), anchor="mm",
        )
        return sheet


def to_png_bytes(image: Image.Image) -> io.BytesIO:
    """디스코드 첨부로 바로 넘길 수 있는 PNG 버퍼로 변환."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer
