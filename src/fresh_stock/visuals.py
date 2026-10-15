"""화면에 붙는 품목 그림과 날씨 그림.

품목 그림은 사람이 정한 표 하나로만 고른다 (디자인 회의 결정: 틀린 그림은 없는 그림보다 나쁘다).
정확히 맞거나 같은 계열(귤류·포도류)일 때만 그림을 쓰고, 맞는 그림이 없으면 분류색 네모를 쓴다.
그림은 Fluent Emoji Flat (MIT), static/produce/ 에 있다.
"""

from .domain import Category, Item

# 품목 이름 → 그림 파일 (static/produce/<파일>.svg)
PRODUCE = {
    "사과": "apple", "아오리": "green-apple", "풋사과": "green-apple",
    "배": "pear",
    "귤": "tangerine", "한라봉": "tangerine", "천혜향": "tangerine", "오렌지": "tangerine",
    "딸기": "strawberry",
    "토마토": "tomato", "방울토마토": "tomato",
    "바나나": "banana",
    "포도": "grapes", "샤인머스캣": "grapes", "거봉": "grapes",
    "복숭아": "peach", "천도": "peach",
    "수박": "watermelon", "키위": "kiwi", "레몬": "lemon", "블루베리": "blueberries", "멜론": "melon",
    "파인애플": "pineapple", "망고": "mango", "체리": "cherries",
    "상추": "leafy-green", "청경채": "leafy-green",
    "양파": "onion", "마늘": "garlic", "깐마늘": "garlic",
    "감자": "potato", "고구마": "sweet-potato", "당근": "carrot",
    "오이": "cucumber", "가지": "eggplant",
    "고추": "hot-pepper", "청양고추": "hot-pepper", "풋고추": "hot-pepper",
    "파프리카": "bell-pepper", "피망": "bell-pepper",
    "브로콜리": "broccoli",
    "새송이": "mushroom", "표고": "mushroom", "느타리": "mushroom", "팽이": "mushroom", "버섯": "mushroom",
    "생강": "ginger",
    "왕란": "egg", "특란": "egg", "계란": "egg", "메추리알": "egg",
    "밤": "chestnut", "옥수수": "corn",
}  # fmt: skip
# 맞는 그림이 없는 기타 품목 중 건어물 (분류 네모 색이 다르다)
DRIED = ("멸치", "새우", "김", "미역", "다시마", "황태", "진미", "오징어", "건어물", "채")
# 로그인 좌판에 쌓는 그림
STALL = ("carrot", "apple", "grapes", "pear", "leafy-green", "tangerine", "tomato", "potato", "egg")


def item_pic(item: Item) -> dict[str, str | None]:
    """{"src": 그림 경로 또는 None, "sq": 그림이 없을 때 분류 네모 클래스}."""
    name = item.name.strip()
    file = PRODUCE.get(name)
    if file:
        return {"src": f"/static/produce/{file}.svg", "sq": None}
    if item.category == Category.FRUIT:
        sq = "fruit"
    elif item.category == Category.VEGETABLE:
        sq = "veg"
    elif any(word in name for word in DRIED):
        sq = "dry"
    else:
        sq = "tofu"
    return {"src": None, "sq": sq}


def weather_pic(text: str) -> str | None:
    """날씨 자동 메모에 붙이는 그림."""
    if "눈" in text:
        return "/static/weather/snow.svg"
    if "비" in text:
        return "/static/weather/rain.svg"
    if "폭염" in text or "더위" in text:
        return "/static/weather/sun.svg"
    return "/static/weather/cloudy.svg"
