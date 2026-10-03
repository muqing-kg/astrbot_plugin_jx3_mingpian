"""门派数据：门派诗、门派场景、门派配色、生图提示词。

配色不写死，从 templates/img/sect/<门派>.png 徽记取主色。
新增门派只需补图标与诗词，配色自动生成。
"""

from __future__ import annotations

import colorsys
from functools import lru_cache
from pathlib import Path

SECT_DIR = Path(__file__).resolve().parent.parent / "templates" / "img" / "sect"

# 门派诗文本来源：公开门派资料整理，缺官方文本的门派为模型补写
SECT_POEM_SOURCE = "公开门派资料整理，部分为模型补写"

# 每个门派四句，渲染时自右向左排（第一句在最右）
SECT_POEMS: dict[str, tuple[str, str, str, str]] = {
    "少林": (
        "古刹紫竹禅钟鸣，",
        "降妖伏魔江湖行。",
        "佛音亦有豪情意，",
        "天下武功出少林。",
    ),
    "七秀": (
        "小桥流水叶娉婷，",
        "画廊绣舫霓裳舞。",
        "楼外楼中雨霖铃，",
        "西子湖畔西子情。",
    ),
    "纯阳": (
        "昆仑玄境山外山，",
        "乾坤阴阳有洞天。",
        "只问真君何处有，",
        "不向江湖寻剑仙。",
    ),
    "万花": (
        "春兰秋菊夏清风，",
        "三星望月挂夜空。",
        "不求独避风雨外，",
        "只笑桃源非梦中。",
    ),
    "天策": (
        "长河落日东都城，",
        "铁马戍边将军坟。",
        "尽诛宵小天策义，",
        "长枪独守大唐魂。",
    ),
    "五毒": (
        "蛇蝎为伴蛛为邻，",
        "千蝶绕笛蛊无形。",
        "世人皆惧断肠物，",
        "不见最毒在人心。",
    ),
    "唐门": (
        "蜀中世家纷争事，",
        "暗起云涌逍九天。",
        "针翎钉棘十指牵，",
        "暴雨飞星乾坤颠。",
    ),
    "藏剑": (
        "秀水灵山隐剑踪，",
        "不闻江湖铸青锋。",
        "逍遥此身君子意，",
        "一壶温酒向长空。",
    ),
    "明教": (
        "白沙大漠玉笛吹，",
        "一去三生渐忘谁。",
        "日月同辉出乱世，",
        "光明圣火盼东归。",
    ),
    "丐帮": (
        "搅动君山五十州，",
        "风尘几历尽翩遥。",
        "散罢千金未束手，",
        "餐风吞酒不寂寥。",
    ),
    "苍云": (
        "歌起征思芦管怨，",
        "透穿玄甲朔风寒。",
        "黄泉作酒酬兄弟，",
        "战尽狂沙血未干。",
    ),
    "长歌": (
        "儒门有志羁风雨，",
        "失鹿山河散若星。",
        "千古文人侠客梦，",
        "肯将碧血写丹青。",
    ),
    "霸刀": (
        "寥落尘寰数十载，",
        "何曾开眼论豪英。",
        "刀光起处鲸吞海，",
        "誓将浮名敬死生。",
    ),
    "蓬莱": (
        "崖岸穿云卷怒涛，",
        "清风三尺削蓬嵩。",
        "古来侠客俱已矣，",
        "天下谁人共萧骚。",
    ),
    "凌雪": (
        "山河覆血风波恶，",
        "吴钩拂拭与君同。",
        "雪满长阶人不语，",
        "一诺平生付剑锋。",
    ),
    "衍天": (
        "衍化星辰布九宫，",
        "天机一脉贯鸿蒙。",
        "世人只道神仙术，",
        "不解胸中万象空。",
    ),
    "药宗": (
        "百草千山采作薪，",
        "一炉风雪一炉春。",
        "但教世上无疾苦，",
        "何惜此身化药尘。",
    ),
    "刀宗": (
        "孤帆万里海云东，",
        "寒刃出鞘裂长风。",
        "此生不问身前事，",
        "只问刀锋敢向空。",
    ),
    "万灵山庄": (
        "万灵同契入烟霞，",
        "山色连云一径斜。",
        "不问人间兵甲事，",
        "听风听雨卧松花。",
    ),
    "段氏": (
        "苍山雪照洱海秋，",
        "剑气如虹贯斗牛。",
        "大理城中花似锦，",
        "一肩风雨一肩收。",
    ),
}

# 同一门派的不同写法，统一映射到 SECT_POEMS / SECT_SCENE 的键
SECT_ALIASES: dict[str, str] = {
    "凌雪阁": "凌雪",
    "衍天宗": "衍天",
    "北天药宗": "药宗",
}

# 门派场景描述，用于生成名片底图
SECT_SCENE: dict[str, str] = {
    "少林": "嵩山少林古刹，千年银杏金黄铺地，青石阶与石灯，晨钟暮鼓，薄雾缭绕的殿宇飞檐",
    "七秀": "扬州瘦西湖畔，垂柳桃花盛放，画舫水榭与曲桥，粉色花瓣随水漂流，朦胧晨光",
    "纯阳": "华山之巅，皑皑积雪与翻涌云海，青石道观与长明灯，苍松挂雪，清冷月色",
    "万花": "秦岭青岩万花谷，层叠花海与竹林药圃，紫藤垂落，奇石流水，幽静雅致",
    "天策": "洛阳东都郊外，大唐军营与猎猎旌旗，玄色铁甲长枪列阵，烽火台与残阳，苍凉肃杀",
    "五毒": "苗疆蛊林深处，幽暗雨林与藤蔓垂落，瘴气弥漫，斑斓异花与石雕图腾，紫黑幽光",
    "唐门": "蜀中唐家堡，层叠机关楼阁与密竹，青铜机括与暗器悬索，青碧色雾气",
    "藏剑": "西湖藏剑山庄，剑池寒潭与接天荷叶，金桂飘落，飞檐水榭，清朗明亮",
    "明教": "西域大漠，连绵沙丘与落日，明教圣火坛与红衣教众剪影，赤金流沙，光明炽烈",
    "丐帮": "洞庭湖君山岛，芦苇荡与湖光，酒坛竹杖散落，渔船灯火，青绿温润",
    "苍云": "雁门关外，朔风卷雪，玄甲苍云军列阵，黑铁城关与烽燧，铁灰冷冽",
    "长歌": "千岛湖畔长歌门，层叠书阁与烟波，青竹掩映，墨卷与古琴，青碧文雅",
    "霸刀": "黄河以北霸刀山庄，刀冢林立与苍岩峭壁，魏晋风骨建筑，靛蓝暮色，肃穆厚重",
    "蓬莱": "东海蓬莱仙岛，悬崖穿云与无垠海天，白鹤盘旋，仙山楼阁隐于雾中，深邃幽蓝",
    "凌雪": "长安城外的雪原，白衣楼阁隐于风雪，寒松与断碑，冷月无声，素白清冷",
    "衍天": "昆仑山巅观星台，浑天仪与星轨图，云海翻涌，穹顶星图流转，深紫幽蓝",
    "药宗": "北疆雪山药王谷，成片药田与温室，晒药架与青铜药炉，飞雪与青烟，青绿温润",
    "刀宗": "东海孤岛刀冢，断崖与海雾，成排插立的旧刀，海天一线，铁灰冷冽",
    "万灵山庄": "西南山林万灵山庄，古木参天与灵兽栖息，藤桥溪涧与石雕图腾，翠绿温润",
    "段氏": "大理苍山洱海，白族楼阁与山茶花，雪峰映湖，剑气凌空，青白明朗",
}

# 底卡提示词：生成场景、人物、两块半透明面板与外圈齿孔，不生成文字类元素
BASE_PROMPT = """Asset type: 剑网3（JX3）游戏角色名片的"无字底卡"，横向
Primary request: 生成一张剑网3角色名片的完整卡面。卡面质感、面板样式、边框、配色参照 Image 1；中央人物使用 Image 2 中那位角色，画成大幅半身像，柔化融入卡面。
Input images:
- Image 1: 卡面样式与质感参考（只借面板、边框、齿孔、玻璃反光、雨痕质感与配色）
- Image 2: 人物参考

门派主题: {school}。场景内容：{scene}
卡面主色: {accent}（只用于卡面面板、边框与场景氛围）

人物约束（最重要，违反则整张作废）:
- 人物的容貌、脸型、五官、发型、发色、发饰、耳饰、额饰、妆容、肤色，必须与 Image 2 中的角色完全一致，是同一个角色
- 人物的服装款式与服装颜色必须与 Image 2 完全一致，不要换装、不要改衣色、不要摘换配饰、不要增减身上的装饰
- 卡面主色 {accent} 只作用于卡面与场景，绝对不要用它去染人物的衣服或头发
- 原图人物身上或手中的任何物件都保持原样

Layout（按画幅百分比，务必对齐）:
- 左侧一块半透明圆角面板：左边距 3.6%，上边距 8%，宽 61%，高 76%，带细点线边框，面板内部保持均匀通透
- 右侧一块较深的半透明圆角面板：左边距 73%，上边距 6.6%，宽 23.5%，高 87%，带细点线边框
- 中央大幅半身人物像，从卡片顶部延伸到卡片底部，位于左面板之上、右面板之下
- 卡面外圈一圈白色邮票齿孔边框
- 面板要有真实的玻璃质感与光影层次，不要画成扁平色块

Style/medium: 国风3D游戏角色渲染，大光圈景深，玻璃反光与雨痕质感，柔光
Constraints:
- 画面中绝对不能出现任何文字、汉字、数字、书法、印章、logo、水印、条码
- 不要画日期；不要画门派徽记；不要画头像或圆形人像框
- 除上述两块面板、人物与齿孔外边框外，不要添加任何其他 UI 元素
"""

# 头像提示词：把角色转成正视镜头的正面头像
AVATAR_PROMPT = """Asset type: 游戏角色名片的圆形头像素材
Primary request: 把输入图中这位角色，转成一张正视镜头的正面标准头像。
Input image: 剑网3角色截图。只取该角色的头部与肩部，忽略原图的姿势与朝向。
Style/medium: 与输入图完全相同的国风3D游戏角色渲染风格、肤质、光照与色调
Composition/framing: 正方形构图，头部居中并占画面主要部分，正视镜头，目光平视前方，表情自然平和，下巴到肩部为止
Constraints:
- 容貌、脸型、五官、发型、发饰、耳饰、额饰、妆容、肤色必须与原图人物完全一致，是同一个角色，不得改变长相或换发型
- 必须正脸朝前，不得侧脸、不得低头抬头、不得有手或头发遮挡面部
- 背景为柔和的浅色虚化，不要任何文字、logo、水印、边框
"""


def canonical_school(school: str) -> str:
    """把门派的别名归一到 SECT_POEMS / SECT_SCENE 使用的键。"""
    name = str(school or "").strip()
    return SECT_ALIASES.get(name, name)


def parse_card_index(raw: object) -> int:
    """把「第几张名片」转成 int。空值或不合法一律抛 ValueError。"""
    text = str(raw or "").strip()
    if not text.isdigit() or int(text) < 1:
        raise ValueError("序号只能是正整数")
    return int(text)


def sect_emblem(school: str) -> Path | None:
    """门派徽记路径，找不到返回 None。"""
    if not school:
        return None
    path = SECT_DIR / f"{school}.png"
    return path if path.exists() else None


def sect_poem(school: str) -> list[str]:
    """门派诗，四句；没有则返回空列表，模板会跳过诗句区。"""
    return list(SECT_POEMS.get(canonical_school(school), ()))


def sect_scene(school: str) -> str:
    """门派场景描述，用于生成底图；没有则用通用描述。"""
    return SECT_SCENE.get(canonical_school(school), "国风游戏场景")


@lru_cache(maxsize=128)
def sect_accent(school: str) -> tuple[str, str]:
    """从门派徽记取主色，返回 (accent, accent_deep) 两个 #rrggbb。

    取不透明像素中饱和度与明度达标的部分，按色相 12 等分投票，
    取票数最高的一档求平均，再抬到可用区间。
    """
    path = sect_emblem(school)
    if path is None:
        return "#8a6aa8", "#4a3560"

    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGBA")
        im.thumbnail((96, 96))
        raw = im.tobytes()

    buckets: dict[int, list[tuple[int, int, int]]] = {}
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a < 128:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.22 or v < 0.18:
            continue
        buckets.setdefault(int(h * 12) % 12, []).append((r, g, b))

    if not buckets:
        return "#8a6aa8", "#4a3560"

    best = max(buckets.values(), key=len)
    r = sum(p[0] for p in best) / len(best) / 255
    g = sum(p[1] for p in best) / len(best) / 255
    b = sum(p[2] for p in best) / len(best) / 255

    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    ar, ag, ab = colorsys.hsv_to_rgb(h, max(s, 0.38), max(v, 0.72))
    dr, dg, db = colorsys.hsv_to_rgb(h, min(1.0, max(s, 0.38) * 1.1), 0.30)
    return (
        f"#{round(ar * 255):02x}{round(ag * 255):02x}{round(ab * 255):02x}",
        f"#{round(dr * 255):02x}{round(dg * 255):02x}{round(db * 255):02x}",
    )


def sect_card_fields(school: str) -> dict:
    """模板需要的门派相关字段。"""
    accent, deep = sect_accent(school)
    return {
        "school": school,
        "schoolIcon": _emblem_data_uri(school),
        "poem": sect_poem(school),
        "accent": accent,
        "accentDeep": deep,
    }


def _emblem_data_uri(school: str) -> str:
    import base64

    path = sect_emblem(school)
    if path is None:
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(
        "ascii"
    )
