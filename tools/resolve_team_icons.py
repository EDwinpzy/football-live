# -*- coding: utf-8 -*-
"""批量补齐 team_icons.json 缺失球队的队标（需求 足球#3 真实队标）。

内置缺队标球队的中→英映射 → TheSportsDB searchteams（节流）→
strBadge URL 校验（仅白名单域 + https + image 内容）→ 合并写回
assets/team_icons.json。查不到/校验失败的球队跳过（保留后端字母徽章兜底）。

用法: python tools\resolve_team_icons.py [--throttle 0.5]
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# 纯字面量路径常量，不含外部输入；resolve 后断言仍在工程目录内
_PROJECT = Path(__file__).resolve().parents[1]
ICONS = _PROJECT / "android" / "app" / "src" / "main" / "assets" / "team_icons.json"
assert ICONS.is_relative_to(_PROJECT), "ICONS 必须位于工程目录内"
UA = "Mozilla/5.0 (compatible; ZuoLive/1.0)"

# SSRF 防护：仅允许这两个固定域名（TDB API + 其 CDN 队标）
ALLOWED_HOSTS = {"www.thesportsdb.com", "r2.thesportsdb.com"}
IMG_HOSTS = {"r2.thesportsdb.com", "crests.football-data.org"}

# 缺失球队 → TheSportsDB 搜索名（英语）。篮球/无法确认的队不收录（app 端本就过滤非足球）。
MISSING_EN = {
    # 日本
    "FC东京": "FC Tokyo", "东京绿茵": "Tokyo Verdy", "京都不死鸟": "Kyoto Sanga",
    "冈山绿雉": "Fagiano Okayama", "名古屋鲸八": "Nagoya Grampus", "千叶市原": "JEF United Chiba",
    "大阪樱花": "Cerezo Osaka", "町田泽维亚": "Machida Zelvia", "清水鼓动": "Shimizu S-Pulse",
    "神户胜利船": "Vissel Kobe", "福冈黄蜂": "Avispa Fukuoka", "鹿岛鹿角": "Kashima Antlers",
    "长崎成功丸": "V-Varen Nagasaki", "水户蜀葵": "Mito HollyHock", "柏太阳神": "Kashiwa Reysol",
    # 韩国
    "光州FC": "Gwangju FC", "仁川联队": "Incheon United", "全北现代": "Jeonbuk Hyundai Motors",
    "大田市民": "Daejeon Hana Citizen", "金泉尚武": "Gimcheon Sangmu", "蔚山HD": "Ulsan HD FC",
    "浦项制铁": "Pohang Steelers", "安养FC": "FC Anyang", "富川FC": "Bucheon FC 1995",
    # 德国
    "RB莱比锡": "RB Leipzig", "不莱梅": "Werder Bremen", "凯泽斯劳滕": "Kaiserslautern",
    "卡尔斯鲁厄": "Karlsruher SC", "比勒菲尔德": "Arminia Bielefeld", "纽伦堡": "Nurnberg",
    "沙尔克04": "Schalke 04", "达姆施塔特": "Darmstadt 98", "马格德堡": "Magdeburg",
    "德累斯顿": "Dynamo Dresden", "菲尔特": "Greuther Furth", "埃弗斯堡": "Elversberg",
    "荷尔斯泰因基尔": "Holstein Kiel", "帕德博恩": "Paderborn", "科特布斯": "Energie Cottbus",
    # 荷兰
    "乌德勒支": "FC Utrecht", "兹沃勒": "PEC Zwolle", "前进之鹰": "Go Ahead Eagles",
    "海伦芬": "Heerenveen", "海牙": "ADO Den Haag", "威廉二世": "Willem II",
    "特尔斯达": "Telstar", "SBV精英队": "Excelsior", "鹿特丹斯巴达": "Sparta Rotterdam",
    # 葡萄牙
    "艾华卡": "Arouca", "阿罗卡": "Arouca", "圣克拉拉": "Santa Clara",
    "葡萄牙国民": "Nacional", "马里迪莫": "Maritimo", "维塞乌": "Academico Viseu",
    # 苏格兰
    "圣约翰斯通": "St Johnstone", "圣米伦": "St Mirren", "邓迪FC": "Dundee FC",
    "邓迪联队": "Dundee United", "马瑟韦尔": "Motherwell", "福尔柯克": "Falkirk",
    "基马诺克": "Kilmarnock", "阿伯丁": "Aberdeen", "格拉斯哥流浪者": "Rangers",
    # 英格兰
    "博尔顿": "Bolton Wanderers", "卡迪夫城": "Cardiff City", "布莱克本": "Blackburn Rovers",
    "布里斯托城": "Bristol City", "斯旺西": "Swansea City", "普雷斯顿": "Preston North End",
    "朴茨茅斯": "Portsmouth", "米尔沃尔": "Millwall", "米德尔斯堡": "Middlesbrough",
    "考文垂": "Coventry City", "诺维奇": "Norwich City", "西布罗姆维奇": "West Bromwich Albion",
    "谢菲尔德联": "Sheffield United", "赫尔城": "Hull City", "女王公园巡游者": "Queens Park Rangers",
    "查尔顿": "Charlton Athletic", "林肯城": "Lincoln City",
    # 意大利
    "蒙扎": "Monza", "弗洛西诺尼": "Frosinone", "切塞纳": "Cesena", "阿斯科利": "Ascoli",
    "阿维利诺": "Avellino", "维琴察": "Vicenza", "帕多瓦": "Padova", "恩特拉": "Virtus Entella",
    # 瑞士
    "巴塞尔": "Basel", "卢加诺": "Lugano", "卢塞恩": "Luzern", "洛桑": "Lausanne",
    "锡永": "Sion", "苏黎世": "Zurich", "草蜢": "Grasshopper Club", "圣加仑": "St. Gallen",
    "图恩": "Thun",
    # 比利时
    "标准列日": "Standard Liege", "安特卫普": "Royal Antwerp", "梅赫伦": "KV Mechelen",
    "沙勒罗瓦": "Charleroi", "色格拉布鲁日": "Cercle Brugge", "拉路维尔": "La Louviere",
    "奥德赫维里": "Oud-Heverlee Leuven", "科特赖克": "Kortrijk", "洛默尔": "Lommel SK",
    # 法国
    "兰斯": "Stade de Reims", "勒芒": "Le Mans", "特鲁瓦": "Troyes", "甘冈": "Guingamp",
    "巴黎FC": "Paris FC", "红星": "Red Star FC", "安尼茨": "Annecy",
    # 北欧
    "布兰": "Brann", "罗森博格": "Rosenborg", "特罗姆瑟": "Tromso", "桑德菲杰": "Sandefjord",
    "萨普斯堡": "Sarpsborg 08", "莫尔德": "Molde", "维京": "Viking Stavanger",
    "利恩比": "Lyngby", "锡尔克堡": "Silkeborg", "奥胡斯": "AGF Aarhus", "欧登塞": "Odense Boldklub",
    "兰德斯": "Randers", "卡尔马": "Kalmar FF", "哈尔姆斯塔德": "Halmstads BK",
    "埃尔夫斯堡": "Elfsborg", "哥德堡": "IFK Goteborg", "索尔纳": "AIK Stockholm",
    "瓦斯特拉斯": "Vasteras SK", "代格福什": "Degerfors", "奥勒松": "Aalesund",
    "汉坎": "HamKam", "克里斯蒂": "Kristiansund BK", "斯达": "IK Start",
    "KFUM奥斯陆": "KFUM Oslo", "奥尔格里特": "Orgryte IS",
    # 波兰
    "克拉科夫": "Wisla Krakow", "克拉科维亚": "Cracovia", "乔治罗尼亚": "Jagiellonia Bialystok",
    "卢宾扎格勒比": "Zaglebie Lubin", "莫托路宾": "Motor Lublin", "皮亚斯特": "Piast Gliwice",
    "琴斯托霍瓦": "Rakow Czestochowa", "GKS卡托威斯": "GKS Katowice", "什切青": "Pogon Szczecin",
    "拉多麦科": "Radomiak Radom",
    # 西班牙
    "拉斯帕尔马斯": "Las Palmas", "莱加内斯": "Leganes", "马拉加": "Malaga", "埃瓦尔": "Eibar",
    "皇家奥维耶多": "Real Oviedo", "阿尔巴切特": "Albacete",
    # 土耳其
    "科尼亚体育": "Konyaspor", "里泽体育": "Rizespor", "哥兹塔比": "Goztepe",
    "加济安泰普大都会": "Gaziantep FK",
    # 中南美
    "克鲁塞罗": "Cruzeiro", "圣保罗": "Sao Paulo", "瓦斯科达伽马": "Vasco da Gama",
    "巴拉纳竞技会": "Athletico Paranaense", "布拉干蒂诺RB": "Red Bull Bragantino",
    "罗萨里奥中央": "Rosario Central", "里奥夸尔托学生队": "Estudiantes de Rio Cuarto",
    "拉普拉塔体操": "Gimnasia y Esgrima La Plata", "利斯特雷": "Deportivo Riestra",
    "克雷塔罗": "Queretaro FC", "瓜达拉哈拉": "Guadalajara Chivas", "桑托斯拉古纳": "Santos Laguna",
    "阿特拉斯": "Atlas FC",
    # 北美
    "休斯敦迪纳摩": "Houston Dynamo", "华盛顿联队": "D.C. United",
    "堪萨斯城竞技": "Sporting Kansas City", "纽约红牛": "New York Red Bulls",
    "科罗拉多急流": "Colorado Rapids", "圣何塞地震": "San Jose Earthquakes",
    "多伦多FC": "Toronto FC", "蒙特利尔": "CF Montreal",
    # 中东
    "哈萨征服": "Al-Fateh", "达曼协作": "Al-Ettifaq",
    # 其他欧洲
    "安道尔FC": "FC Andorra", "阿马多拉": "Estrela Amadora",
    # 国家队
    "中国香港": "Hong Kong", "保加利亚": "Bulgaria", "喀麦隆": "Cameroon", "埃及": "Egypt",
    "斐济": "Fiji", "蒙古": "Mongolia", "尼泊尔": "Nepal", "马来西亚": "Malaysia",
    "新加坡": "Singapore", "吉尔吉斯斯坦": "Kyrgyzstan", "巴勒斯坦": "Palestine",
    "安哥拉": "Angola", "刚果民主共和国": "DR Congo", "佛得角": "Cape Verde",
    "北马其顿": "North Macedonia", "几内亚": "Guinea", "塞内加尔": "Senegal",
    "哈萨克斯坦": "Kazakhstan", "尼日利亚": "Nigeria", "科特迪瓦": "Ivory Coast",
    "马里": "Mali", "马尔代夫": "Maldives", "菲律宾": "Philippines",
    # 中国俱乐部（TDB 覆盖不全，查得到就补）
    "南通支云": "Nantong Zhiyun", "青岛海牛": "Qingdao Hainiu", "延边龙鼎": "Yanbian Longding",
    "石家庄功夫": "Shijiazhuang Gongfu", "苏州东吴": "Suzhou Dongwu",
    "陕西联合": "Shaanxi United", "辽宁铁人": "Liaoning Tieren",
    "重庆铜梁龙": "Chongqing Tonglianglong", "河南队": "Henan", "大连鲲城": "Dalian Kuncheng",
}

API_HOST = "www.thesportsdb.com"


def _check_url(u: str, hosts) -> bool:
    sp = urlsplit(u)
    return sp.scheme == "https" and sp.hostname in hosts


def tdb_search(en: str):
    """搜索球队，返回白名单域内的队标 URL（其余一律拒绝）。"""
    url = "https://%s/api/v1/json/3/searchteams.php?t=%s" % (API_HOST, urllib.parse.quote(en.replace(" ", "_")))
    if not _check_url(url, ALLOWED_HOSTS):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=6) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    teams = data.get("teams") or []
    for t in teams:
        if (t.get("strSport") or "") != "Soccer":
            continue
        badge = t.get("strBadge") or t.get("strTeamBadge")
        if badge and _check_url(badge, IMG_HOSTS):
            return badge
    return None


def url_ok(u: str) -> bool:
    if not _check_url(u, IMG_HOSTS):
        return False
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            ct = r.headers.get("Content-Type", "")
            head = r.read(8)
            return r.status == 200 and ("image" in ct or head[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"<svg"))
    except Exception:
        return False


def main():
    throttle = 0.5
    if "--throttle" in sys.argv:
        throttle = float(sys.argv[sys.argv.index("--throttle") + 1])
    icons = json.loads(ICONS.read_text(encoding="utf-8"))
    added, failed = [], []
    for cn, en in MISSING_EN.items():
        if cn in icons:
            continue
        try:
            badge = tdb_search(en)
        except Exception:
            badge = None
        time.sleep(throttle)
        if not badge:
            failed.append((cn, en, "no-result"))
            continue
        if not url_ok(badge):
            failed.append((cn, en, "badge-url-fail"))
            time.sleep(throttle)
            continue
        icons[cn] = badge
        added.append((cn, badge))
        print("OK  %-14s -> %s" % (cn, badge), flush=True)
    ICONS.write_text(json.dumps(icons, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print("\n== 新增 %d 个，失败 %d 个 ==" % (len(added), len(failed)))
    for cn, en, why in failed:
        print("FAIL %-14s (%s): %s" % (cn, en, why))


if __name__ == "__main__":
    main()
