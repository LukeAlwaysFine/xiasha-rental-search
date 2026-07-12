"""Deep dive 区域关键词配置 — 每个目标区域一个配置条目。

生成策略：MTOP API 分页不可用（sign 校验），每关键词只能拿第一页 ~30 条。
因此用更多细粒度关键词覆盖同一区域，关键词多样性直接决定去重后的唯一结果数。

添加新区域：在 AREA_CONFIG 中新增一个条目，复用相同的 build_area_keywords() 逻辑。
"""

# 通用租类型（所有区域共享）
_RENT_TYPES = ["租房", "转租", "整租", "合租", "单间"]


AREA_CONFIG: dict[str, dict] = {
    "下沙": {
        # 下沙子区域/商圈（内部地理划分）
        "sub_areas": [
            "下沙江滨", "金沙湖", "高沙", "文泽路", "文海南路",
            "云水", "下沙沿江", "下沙大学城北", "下沙物美",
            "下沙银泰", "福雷德", "白杨", "下沙西",
        ],
        # 地铁 1 号线站点（搜索时与子区域可能有重叠但 MTOP 搜索结果不同）
        "metro_stations": [
            "金沙湖站", "高沙路站", "文泽路站", "文海南路站",
            "云水站", "下沙江滨站", "下沙西站",
        ],
        # 下沙已知小区（最细粒度，每个小区名是独立搜索词）
        "compounds": [
            "东沙铭城", "东岸嘉园", "金沙湖壹号", "和达城",
            "阳光华城", "头格月雅城", "春风金沙", "东城大厦",
            "和达自由港", "世茂江滨花园", "朗诗国际街区", "观澜时代",
            "伊萨卡国际城", "宋都晨光国际", "野风海天城", "梦琴湾",
            "金沙阳光", "滟澜山", "龙湖天街公寓", "中豪七格",
            "新沙家园", "高沙小区", "元成时代", "保利东湾",
            "德信早城", "湖左岸", "四季风景苑", "北银公寓",
            "月雅苑", "多蓝水岸", "海天城", "盛泰名都",
            "文苑风情", "香榭里花园", "清雅苑", "金沙学府",
            "锦上文澜", "望金沙", "绿城春风金沙",
        ],
        # 下沙高校（大学城区域）
        "universities": [
            "浙江理工大学", "杭州电子科技大学", "浙江工商大学",
            "中国计量大学", "浙江传媒学院", "杭州师范大学下沙",
        ],
        # 豆瓣搜索 URL（用于 Firecrawl 定向抓取）
        "douban_queries": [
            "https://www.douban.com/group/search?cat=1013&q=下沙租房",
            "https://www.douban.com/group/search?cat=1013&q=下沙转租",
            "https://www.douban.com/group/search?cat=1013&q=金沙湖租房",
            "https://www.douban.com/group/search?cat=1013&q=下沙合租",
        ],
        # 额外的用户常用搜索词
        "extra_keywords": [
            "下沙个人转租", "下沙房东直租", "下沙急转", "下沙短租",
            "下沙整租一室", "下沙整租两室", "下沙无中介",
        ],
    },
    # 未来扩展（模板）：
    # "滨江": {
    #     "sub_areas": ["滨江区政府", "西兴", "长河", "浦沿", "彩虹城", ...],
    #     "metro_stations": [...],
    #     "compounds": [...],
    #     "universities": [...],
    #     "douban_queries": [...],
    #     "extra_keywords": [...],
    # },
}


def build_area_keywords(area_name: str) -> list[str]:
    """为深潜目标区域生成闲鱼搜索关键词。

    关键词层级：
    1. 基础词："{area} {rent_type}"（5 个）
    2. 子区域 × 租类型："杭州{sub} {type}"（保留杭州前缀以减少歧义）
    3. 地铁站 × 租房
    4. 小区名 × 租房/转租
    5. 高校 × 租房
    6. 额外自定义词

    所有关键词以 set 去重后返回。

    Args:
        area_name: 区域名，必须在 AREA_CONFIG 中存在

    Returns:
        去重后的关键词列表。如果 area_name 未配置，返回空列表。
    """
    cfg = AREA_CONFIG.get(area_name)
    if not cfg:
        return []

    keywords: set[str] = set()

    # Tier 1: 基础词
    for t in _RENT_TYPES:
        keywords.add(f"{area_name} {t}")
        keywords.add(f"杭州{area_name} {t}")

    # Tier 2: 子区域 × 租类型（只用前 3 种类型避免爆炸）
    for sub in cfg.get("sub_areas", []):
        for t in _RENT_TYPES[:3]:  # 租房, 转租, 整租
            keywords.add(f"杭州{sub} {t}")
            keywords.add(f"{sub} {t}")

    # Tier 3: 地铁站
    for station in cfg.get("metro_stations", []):
        keywords.add(f"{station} 租房")
        keywords.add(f"杭州{station} 租房")

    # Tier 4: 小区名（只用租房和转租）
    for comp in cfg.get("compounds", []):
        keywords.add(f"{comp} 租房")
        keywords.add(f"{comp} 转租")

    # Tier 5: 高校
    for uni in cfg.get("universities", []):
        keywords.add(f"{uni} 租房")
        keywords.add(f"{uni} 转租")

    # Tier 6: 额外关键词
    for ek in cfg.get("extra_keywords", []):
        keywords.add(ek)

    return list(keywords)


def get_douban_queries(area_name: str) -> list[str]:
    """获取目标区域的豆瓣搜索 URL 列表。"""
    cfg = AREA_CONFIG.get(area_name)
    return cfg.get("douban_queries", []) if cfg else []
