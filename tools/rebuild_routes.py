#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
장학복지팀 오시는 길 — 경로 / 배경지도 데이터 재생성 스크립트

무엇을 하나요?
  1) OpenStreetMap에서 계명대 성서캠퍼스 일대 도로·건물 데이터를 내려받고
  2) 보행/차량 네트워크에서 최단 경로를 직접 계산하고
  3) 타일 서버 없이도 지도가 보이도록 배경 벡터를 단순화해서
  4) index.html 안의 ROUTE_DATA / BASEMAP 값을 통째로 교체합니다.

언제 쓰나요?
  - 출발지·목적지를 바꾸고 싶을 때 (아래 PLACES 수정)
  - 캠퍼스 도로가 바뀌어 경로를 다시 뽑아야 할 때

사용법
  python rebuild_routes.py              # 저장된 osm_raw.json 사용 (인터넷 불필요)
  python rebuild_routes.py --fetch      # OSM에서 최신 데이터를 새로 받아옴

주의
  - 좌표는 (위도, 경도) 순서입니다.
  - 이 스크립트는 좌표만 바꿉니다. 말풍선 문구·단계 설명은
    index.html 안의 META 객체에서 직접 고치세요.
"""

import json, math, heapq, argparse, re, os, sys, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
RAW  = os.path.join(HERE, "osm_raw.json")
HTML = os.path.join(HERE, "..", "index.html")

# ─────────────────────────────────────────────────────────────
# 여기만 고치면 됩니다
# ─────────────────────────────────────────────────────────────
BAUER = (35.854248, 128.4861048)   # 도착지: 바우어관 (장학복지팀 2층 1220호)
GATE  = (35.852625, 128.487141)    # 계명대 정문  ※ 버스정류장 '계명대학교정문앞'과 다름
SOUTH = (35.852660, 128.485054)    # 남문 방면

PLACES = {
    # 이름          출발 좌표                 이동수단  속도(m/분)  경유지
    "gyemyeong": ((35.851474, 128.491956), "foot",  67, None),   # 2호선 계명대역
    "gangchang": ((35.853091, 128.478069), "foot",  67, None),   # 2호선 강창역
    "bus":       ((35.852550, 128.485212), "foot",  67, GATE),   # 계명대학교정문앞 정류장
    "car":       ((35.851680, 128.490900), "drive", 330, GATE),  # 달구벌대로 → 정문 진입
}

BBOX = (35.8475, 128.4745, 35.8595, 128.4950)   # 남, 서, 북, 동

# 도보로 통행 가능한 길 / 차량이 다닐 수 있는 길
FOOT  = {"footway","path","pedestrian","steps","living_street","residential",
         "service","unclassified","tertiary","secondary","primary","track","cycleway"}
DRIVE = {"living_street","residential","service","unclassified",
         "tertiary","secondary","primary"}
# 아래 종류는 걷기 편한 길이라 가중치를 주지 않음(=선호), 나머지는 1.2배 페널티
SOFT  = {"footway","path","pedestrian","steps","service","living_street"}


# ─────────────────────────────────────────────────────────────
def fetch_osm():
    """OSM Overpass API에서 도로·건물·녹지 데이터를 내려받습니다."""
    q = f"""[out:json][timeout:90];
(
  way({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]})["highway"];
  way({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]})["building"];
  way({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]})["leisure"~"^(park|pitch|garden)$"];
  way({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]})["landuse"~"^(grass|forest|recreation_ground)$"];
  way({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]})["natural"="water"];
);out geom;"""
    print("OSM에서 지도 데이터를 받는 중… (30초쯤 걸립니다)")
    req = urllib.request.Request(
        "https://overpass-api.de/api/interpreter",
        data=urllib.parse.urlencode({"data": q}).encode(),
        headers={"User-Agent": "kmu-scholarship-directions/1.0"})
    data = json.load(urllib.request.urlopen(req, timeout=120))
    json.dump(data, open(RAW, "w"), ensure_ascii=False)
    print(f"  → {len(data['elements'])}개 저장: {RAW}")
    return data


def meters(a, b):
    """두 좌표 사이 거리(m)."""
    dy = (a[0]-b[0]) * 111320
    dx = (a[1]-b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def build_graph(ways, allow):
    """도로 목록으로 최단경로 계산용 그래프를 만듭니다."""
    g = {}
    for w in ways:
        hw = w.get("tags", {}).get("highway")
        if hw not in allow:
            continue
        pen = 1.0 if hw in SOFT else 1.2
        pts = [(round(p["lat"], 6), round(p["lon"], 6)) for p in w["geometry"]]
        for a, b in zip(pts, pts[1:]):
            d = meters(a, b) * pen
            g.setdefault(a, {})[b] = min(g.setdefault(a, {}).get(b, 1e9), d)
            g.setdefault(b, {})[a] = min(g.setdefault(b, {}).get(a, 1e9), d)
    return g


def shortest(g, start, goal):
    """다익스트라로 start→goal 최단 경로를 찾습니다."""
    src = min(g, key=lambda n: meters(n, start))
    dist, prev, pq = {src: 0}, {}, [(0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18):
            continue
        for v, w in g[u].items():
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    # 목적지에 충분히 가까운 지점 중 가장 효율적인 곳에서 끊습니다
    for limit in (45, 90, 180, 400):
        near = [n for n in dist if meters(n, goal) <= limit]
        if near:
            break
    else:
        raise RuntimeError("목적지 근처에 도달하는 경로를 찾지 못했습니다")
    end = min(near, key=lambda n: dist[n] + meters(n, goal))
    path = [end]
    while path[-1] != src:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def route_via(g, start, goal, via=None):
    """경유지가 있으면 두 구간을 이어 붙입니다."""
    legs = [shortest(g, start, via), shortest(g, via, goal)] if via \
           else [shortest(g, start, goal)]
    out = list(legs[0])
    for leg in legs[1:]:
        for p in leg:
            if p != out[-1]:
                out.append(p)
    if meters(out[-1], goal) > 10:
        out.append(goal)
    return out


def simplify(pts, eps):
    """Ramer–Douglas–Peucker: 모양을 유지하며 좌표 개수를 줄입니다."""
    if len(pts) < 3:
        return pts
    s, e = pts[0], pts[-1]
    worst, idx = 0, 0
    for i in range(1, len(pts)-1):
        p = pts[i]
        den = (e[0]-s[0])**2 + (e[1]-s[1])**2
        t = 0 if den < 1e-12 else max(0, min(1,
            ((p[0]-s[0])*(e[0]-s[0]) + (p[1]-s[1])*(e[1]-s[1])) / den))
        d = meters(p, (s[0]+t*(e[0]-s[0]), s[1]+t*(e[1]-s[1])))
        if d > worst:
            worst, idx = d, i
    if worst > eps:
        return simplify(pts[:idx+1], eps)[:-1] + simplify(pts[idx:], eps)
    return [s, e]


def build_basemap(ways, routes):
    """타일 서버 없이도 보이도록 배경 도로·건물을 벡터로 뽑습니다."""
    allpts = [p for r in routes.values() for p in r["pts"]]
    m = 0.0016
    bb = (min(p[0] for p in allpts)-m, min(p[1] for p in allpts)-m*1.24,
          max(p[0] for p in allpts)+m, max(p[1] for p in allpts)+m*1.24)

    def inside(geom):
        return any(bb[0] <= p["lat"] <= bb[2] and bb[1] <= p["lon"] <= bb[3]
                   for p in geom)

    def coords(geom, eps):
        pts = simplify([(p["lat"], p["lon"]) for p in geom], eps)
        return [[round(a, 5), round(b, 5)] for a, b in pts]

    out = {"major": [], "minor": [], "foot": [], "bldg": [], "green": []}
    for w in ways:
        t, geom = w.get("tags", {}), w["geometry"]
        if not inside(geom):
            continue
        if "building" in t:
            c = coords(geom, 3.5)
            if len(c) >= 4:
                out["bldg"].append(c)
        elif (t.get("leisure") in ("park", "pitch", "garden")
              or t.get("landuse") in ("grass", "forest", "recreation_ground")
              or t.get("natural") == "water"):
            c = coords(geom, 5)
            if len(c) >= 4:
                out["green"].append(c)
        elif "highway" in t:
            hw = t["highway"]
            if hw in ("primary", "secondary", "trunk", "tertiary"):
                out["major"].append(coords(geom, 4))
            elif hw in ("footway", "path", "pedestrian", "steps", "cycleway"):
                out["foot"].append(coords(geom, 4))
            elif hw in ("residential", "service", "unclassified",
                        "living_street", "track"):
                out["minor"].append(coords(geom, 4))
    return out


def inject(html_path, routes, basemap):
    """index.html 안의 ROUTE_DATA / BASEMAP 값을 교체합니다."""
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    for name, value in (("ROUTE_DATA", routes), ("BASEMAP", basemap)):
        blob = json.dumps(value, separators=(",", ":"))
        html, n = re.subn(rf"const {name}=\{{.*?\}};",
                          f"const {name}={blob};", html, count=1, flags=re.S)
        if n != 1:
            raise RuntimeError(f"index.html에서 {name} 선언을 찾지 못했습니다")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="OSM에서 지도 데이터를 새로 내려받습니다")
    args = ap.parse_args()

    if args.fetch or not os.path.exists(RAW):
        data = fetch_osm()
    else:
        data = json.load(open(RAW, encoding="utf-8"))
        print(f"저장된 지도 데이터 사용: {RAW}")

    ways = [e for e in data["elements"]
            if e["type"] == "way" and "geometry" in e]
    graphs = {"foot": build_graph(ways, FOOT), "drive": build_graph(ways, DRIVE)}

    routes = {}
    print("\n경로 계산")
    for name, (start, mode, speed, via) in PLACES.items():
        pts = route_via(graphs[mode], start, BAUER, via)
        total = sum(meters(a, b) for a, b in zip(pts, pts[1:]))
        routes[name] = {"pts": [[round(a, 6), round(b, 6)] for a, b in pts]}
        # 출발점을 뺀 나머지 구간이 어느 문을 지나는지 (30m 이내면 통과로 봄)
        rest = pts[1:]
        passed = [label for label, pos in (("정문", GATE), ("남문", SOUTH))
                  if min(meters(p, pos) for p in rest) <= 30]
        via_txt = " · ".join(passed) + " 경유" if passed else "교내 직진"
        print(f"  {name:10} {len(pts):2d}점 / {total:4.0f}m / "
              f"약 {max(1, round(total/speed))}분 / {via_txt}")

    basemap = build_basemap(ways, routes)
    print("\n배경 지도")
    for k, v in basemap.items():
        print(f"  {k:6} {len(v):4d}개")

    inject(HTML, routes, basemap)
    print(f"\n완료 — {os.path.normpath(HTML)} 갱신됨")
    print("말풍선 문구·단계 설명은 index.html의 META 객체에서 고치세요.")


if __name__ == "__main__":
    main()
