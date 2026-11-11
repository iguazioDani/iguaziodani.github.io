#!/usr/bin/env python3
"""Builds the MESS website from the RSS.com feed.

Usage:
  python3 build.py                 # fetch the live feed, write the site to _site/
  python3 build.py --feed FILE     # build from a local copy of the feed

Standard library only, so GitHub Actions needs nothing installed.
Edit site.json for copy and links; edit static/style.css for the look.
Static pages (static/*.html) are served as written, except for an episode list placed between
<!-- episode-list:start --> and <!-- episode-list:end --> markers, which every build refills from the feed,
and RSS.com show-cover addresses, which every build points at the feed's current cover.
The homepage cover is drawn and animated by this file (see "animated cover" below).
"""
import argparse, email.utils, hashlib, html, json, re, shutil, sys, urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "_site"
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
CFG = json.loads((ROOT / "site.json").read_text())
BASE_URL = f"https://{CFG['domain']}"
esc = html.escape
# Changes whenever style.css changes, so browsers fetch the new file instead of a saved copy.
CSS_VERSION = hashlib.sha1((ROOT / "static" / "style.css").read_bytes()).hexdigest()[:8]


# ---------- feed ----------

class Sanitizer(HTMLParser):
    """Keeps a small set of safe tags from episode descriptions."""
    OK = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "a"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.OK:
            return
        if tag == "a":
            href = dict(attrs).get("href", "")
            if not href.startswith(("http://", "https://", "mailto:")):
                return
            self.out.append(f'<a href="{esc(href)}" rel="noopener">')
        else:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in self.OK and tag != "br":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(esc(data, quote=False))


def clean_html(raw):
    s = Sanitizer()
    s.feed(raw or "")
    out = "".join(s.out).strip()
    out = re.sub(r"<p>\s*</p>", "", out)
    if out and not out.startswith("<"):
        out = f"<p>{out}</p>"
    return out


def plain(raw, limit=None):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def slugify(s):
    s = re.sub(r"['\u2019]", "", s.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > 60:
        s = s[:60].rsplit("-", 1)[0]
    return s


def fmt_duration(val):
    if not val:
        return ""
    if ":" in val:
        parts = [int(p) for p in val.split(":")]
        secs = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    else:
        secs = int(float(val))
    return f"{round(secs / 60)} min"


def load_feed(path=None):
    if path:
        data = Path(path).read_bytes()
    else:
        req = urllib.request.Request(CFG["feed_url"], headers={"User-Agent": "mess-site-builder"})
        data = urllib.request.urlopen(req, timeout=30).read()
    ch = ET.fromstring(data).find("channel")
    img = ch.find("itunes:image", NS)
    show = {
        "title": ch.findtext("title", ""),
        "description": plain(ch.findtext("description", "")),
        "image": img.get("href") if img is not None else "",
    }
    eps = []
    items = ch.findall("item")
    for i, it in enumerate(items):
        enc = it.find("enclosure")
        num = it.findtext("itunes:episode", namespaces=NS)
        num = int(num) if num and num.strip().isdigit() else None
        ep_type = (it.findtext("itunes:episodeType", namespaces=NS) or "full").lower()
        season = it.findtext("itunes:season", namespaces=NS)
        season = int(season) if season and season.strip().isdigit() else None
        date = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
        title = it.findtext("title", "").strip()
        desc = it.findtext("description", "")
        slug = f"{num}-{slugify(title)}" if num else slugify(title) or f"episode-{len(items) - i}"
        eps.append({
            "num": num, "type": ep_type, "season": season, "title": title, "date": date,
            "date_str": date.strftime("%B %-d, %Y"), "date_short": date.strftime("%b %-d, %Y"),
            "iso": date.isoformat(), "duration": fmt_duration(it.findtext("itunes:duration", namespaces=NS)),
            "audio": enc.get("url") if enc is not None else "",
            "html": clean_html(desc), "summary": plain(desc, 220), "meta": plain(desc, 155),
            "slug": slug, "url": f"/episodes/{slug}/",
        })
    eps.sort(key=lambda e: e["date"], reverse=True)
    return show, eps


# ---------- templates ----------

def listen_links(cls="listen"):
    return f'<ul class="{cls}">' + "".join(
        f'<li><a href="{esc(l["url"])}" rel="noopener">{esc(l["name"])}</a></li>' for l in CFG["listen"]
    ) + "</ul>"


# Counts the first play and the end of each audio/video player on a page, labeled by episode.
MEDIA_TRACKING = """<script>
(function () {
  function send(name, el) {
    if (window.umami && el.dataset.episode) window.umami.track(name, { episode: el.dataset.episode });
  }
  function on(type, flag, verb) {
    document.addEventListener(type, function (e) {
      var el = e.target;
      if (!(el instanceof HTMLMediaElement) || el.dataset[flag]) return;
      el.dataset[flag] = "1";
      send((el.tagName === "VIDEO" ? "Video " : "Audio ") + verb, el);
    }, true);
  }
  on("play", "counted", "play");
  on("ended", "counted_end", "finished");
})();
</script>"""


def analytics_tags():
    """Umami analytics (site.json "umami_website_id"). Only counts visits on the real domain."""
    wid = CFG.get("umami_website_id")
    if not wid:
        return ""
    return (f'<script defer src="https://cloud.umami.is/script.js" data-website-id="{esc(wid)}" '
            f'data-domains="{esc(CFG["domain"])}"></script>\n{MEDIA_TRACKING}')


def page(title, body, desc, path, image, extra_head=""):
    full_title = f"{title} | {CFG['title']}" if title != CFG["title"] else f"{CFG['title']} ({CFG['short_title']})"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{BASE_URL}{path}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{BASE_URL}{path}">
<meta property="og:image" content="{esc(image)}">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="alternate" type="application/rss+xml" title="{esc(CFG['title'])}" href="{esc(CFG['feed_url'])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&family=Source+Serif+4:ital,opsz,wght@0,8..60,400..700;1,8..60,400..700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css?v={CSS_VERSION}">
{analytics_tags()}
{extra_head}
</head>
<body>
<header class="topbar">
  <a class="wordmark" href="/" aria-label="MESS home">MESS</a>
  <nav><a href="/episodes/">Episodes</a>{'<a href="/contact/">Contact</a>' if contact_on() else ''}</nav>
</header>
{body}
<footer class="foot">
  <div class="foot-inner">
    <p class="foot-mark">MESS</p>
    {listen_links("listen listen-foot")}
    <p class="small">&copy; {__import__('datetime').date.today().year} Daniella Alpher &amp; Yossi Alpher</p>
  </div>
</footer>
</body>
</html>
"""


def media_label(ep):
    """How an episode is named in the analytics: "Ep. 10", or the title if it has no number."""
    return ep_label(ep) if ep["num"] else ep["title"]


def player(ep):
    if not ep["audio"]:
        return ""
    return f'<audio controls preload="none" src="{esc(ep["audio"])}" data-episode="{esc(media_label(ep))}"></audio>'


def ep_videos(ep):
    """Videos listed in site.json "videos" for this episode, shown under the show notes."""
    out = []
    for v in CFG.get("videos", []):
        if not v.get("file") or ep["num"] is None or str(v.get("episode")) != str(ep["num"]):
            continue
        attrs = "".join(f' {k}="{esc(str(v[k]))}"' for k in ("poster", "width", "height") if v.get(k))
        cap = f"<figcaption>{esc(v['caption'])}</figcaption>" if v.get("caption") else ""
        out.append(f'<figure class="ep-video"><video controls playsinline preload="metadata" '
                   f'src="{esc(v["file"])}"{attrs} data-episode="{esc(media_label(ep))}"></video>{cap}</figure>')
    return "".join(out)


def ep_label(ep):
    if ep["num"]:
        return f"Ep. {ep['num']}"
    return "Bonus" if ep["type"] == "bonus" else "Trailer" if ep["type"] == "trailer" else ""


def ep_row(ep):
    return f"""<li class="ep-row">
  <a href="{ep['url']}">
    <span class="ep-num">{esc(ep_label(ep))}</span>
    <span class="ep-main"><span class="ep-title">{esc(ep['title'])}</span>
    <span class="ep-sum">{esc(ep['summary'])}</span></span>
    <span class="ep-meta"><span>{ep['date_short']}</span><span>{ep['duration']}</span></span>
  </a>
</li>"""


def host_html(h, tag):
    paras = h["bio"] if isinstance(h["bio"], list) else [h["bio"]]
    def fmt(x):
        x = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", esc(x))
        return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', x)
    bio = "".join("<p>" + fmt(x) + "</p>" for x in paras)
    link = ""
    if h.get("link"):
        link = f'<p class="host-link"><a href="{esc(h["link"]["url"])}" rel="noopener">{esc(h["link"]["label"])}</a></p>'
    photo = ""
    if h.get("photo"):
        photo = f'<img class="host-photo" src="{esc(h["photo"]["src"])}" alt="{esc(h["photo"]["alt"])}" loading="lazy">'
    return f'<div class="host"><p class="kicker">{esc(h["role"])}</p><{tag}>{esc(h["name"])}</{tag}>{bio}{link}{photo}</div>'


def contact_on():
    return bool(CFG.get("contact", {}).get("formspree_id"))



# ---------- animated cover (homepage) ----------
# The homepage draws the cover live instead of showing the image from the feed: the lettering and the
# hand-drawn strokes below are the approved cover, and the script plays game after game on its board.
# It was made for one specific cover file. If a different cover is uploaded to RSS.com, the homepage
# goes back to showing the feed's image, still, until this drawing is redone for the new cover
# (then put the new file name in ANIMATED_COVER). To switch the animation off, set ANIMATED_COVER = "".
ANIMATED_COVER = "podcast_cover_20261109_090425_9a21443673b4411df185831657e21a04"

COVER_SVG = """<svg class="cover cover-live" viewBox="0 0 3000 3000" width="360" height="360" role="img" aria-label="MESS podcast cover art" style="background:none;height:auto">
<path fill="#16140f" d="M481 776.4V212.2H711.8L753.4 411.1Q756.3 424.4 761.1 448.6Q765.8 472.9 771.3 499.4Q776.7 526 780.9 547.9H788.2Q791.5 531 796.3 507Q801.1 482.9 806.1 457.6Q811.1 432.4 814.9 411.4L855.7 212.2H1079.4V776.4H921.1V592.4Q921.1 554.2 922.1 518.4Q923.1 482.6 924.1 453.5Q925.1 424.5 925.1 408.3H918.1Q916.1 421.6 912.5 439.6Q909 457.7 905.4 475Q901.9 492.2 898.5 504.8L839.1 776.4H713.9L654.1 504.8Q651.6 492.2 647.6 475Q643.6 457.7 640.5 439.6Q637.4 421.6 634.9 408.7H627.9Q629.1 429.2 630.1 460.2Q631.1 491.2 631.8 525.6Q632.4 560 632.4 592.4V776.4ZM1166 776.4V212.2H1576.2V344.8H1332.5V425.4H1538.9V555H1332.5V643.8H1581.2V776.4ZM1857.7 786.2Q1809.6 786.2 1767.9 778.2Q1726.2 770.2 1694.5 750Q1662.8 729.9 1644.7 694Q1626.7 658 1626.7 602.2Q1626.7 600.6 1626.7 596.1Q1626.7 591.7 1627.5 588.4H1785.3Q1784.9 592.1 1784.9 595.8Q1784.9 599.4 1784.9 602.6Q1784.9 625.7 1792.9 637.6Q1800.8 649.6 1814.9 653.7Q1829.1 657.8 1846.8 657.8Q1854.7 657.8 1862.4 656.8Q1870.2 655.8 1877.1 653.2Q1884.1 650.6 1889.6 646.1Q1895.1 641.6 1898.1 634.8Q1901 628.1 1901 618.9Q1901 603.4 1889.8 593.6Q1878.6 583.8 1859.7 576.4Q1840.8 569.1 1817.1 562.4Q1793.4 555.7 1767.8 547.3Q1742.2 538.9 1718.5 526.4Q1694.8 513.9 1675.9 494.9Q1657 475.8 1645.8 448Q1634.5 420.3 1634.5 381.6Q1634.5 332.4 1651.9 298.2Q1669.3 263.9 1699.8 242.8Q1730.2 221.7 1768.7 211.8Q1807.2 202 1849.5 202Q1892.2 202 1929.7 211.3Q1967.2 220.6 1995.8 240.3Q2024.4 260.1 2040.8 291.7Q2057.2 323.4 2057.6 368.1V384.5H1901.4V378.1Q1901.4 363.8 1897 352.4Q1892.5 341 1882.1 334.4Q1871.8 327.7 1853.3 327.7Q1836.5 327.7 1825.3 332.3Q1814.1 336.8 1809 344.6Q1803.9 352.4 1803.9 362.3Q1803.9 378.6 1815.2 389.7Q1826.4 400.7 1845.5 408.2Q1864.6 415.7 1888.3 422.4Q1912 429.1 1937.4 437.1Q1962.8 445 1986.5 456.9Q2010.2 468.8 2029.3 487Q2048.4 505.3 2059.6 531.6Q2070.8 557.8 2070.8 595.3Q2070.8 661.1 2043.9 703.4Q2016.9 745.7 1969.1 765.9Q1921.3 786.2 1857.7 786.2ZM2341.4 786.2Q2293.2 786.2 2251.5 778.2Q2209.9 770.2 2178.1 750Q2146.4 729.9 2128.4 694Q2110.3 658 2110.3 602.2Q2110.3 600.6 2110.3 596.1Q2110.3 591.7 2111.2 588.4H2268.9Q2268.5 592.1 2268.5 595.8Q2268.5 599.4 2268.5 602.6Q2268.5 625.7 2276.5 637.6Q2284.5 649.6 2298.6 653.7Q2312.7 657.8 2330.4 657.8Q2338.3 657.8 2346.1 656.8Q2353.8 655.8 2360.8 653.2Q2367.7 650.6 2373.2 646.1Q2378.7 641.6 2381.7 634.8Q2384.7 628.1 2384.7 618.9Q2384.7 603.4 2373.5 593.6Q2362.2 583.8 2343.3 576.4Q2324.4 569.1 2300.7 562.4Q2277 555.7 2251.4 547.3Q2225.9 538.9 2202.2 526.4Q2178.5 513.9 2159.6 494.9Q2140.6 475.8 2129.4 448Q2118.2 420.3 2118.2 381.6Q2118.2 332.4 2135.6 298.2Q2153 263.9 2183.4 242.8Q2213.8 221.7 2252.3 211.8Q2290.9 202 2333.1 202Q2375.8 202 2413.3 211.3Q2450.8 220.6 2479.4 240.3Q2508 260.1 2524.4 291.7Q2540.8 323.4 2541.2 368.1V384.5H2385.1V378.1Q2385.1 363.8 2380.6 352.4Q2376.1 341 2365.8 334.4Q2355.4 327.7 2337 327.7Q2320.1 327.7 2308.9 332.3Q2297.8 336.8 2292.7 344.6Q2287.6 352.4 2287.6 362.3Q2287.6 378.6 2298.8 389.7Q2310 400.7 2329.1 408.2Q2348.3 415.7 2372 422.4Q2395.7 429.1 2421 437.1Q2446.4 445 2470.1 456.9Q2493.8 468.8 2512.9 487Q2532 505.3 2543.3 531.6Q2554.5 557.8 2554.5 595.3Q2554.5 661.1 2527.5 703.4Q2500.6 745.7 2452.7 765.9Q2404.9 786.2 2341.4 786.2Z"/>
<path fill="#16140f" d="M505 1012.4V924.5H534.1L546.7 969.5Q547.4 971.9 548.2 975.2Q549 978.6 549.8 981.9Q550.6 985.1 551.1 987.7H552.1Q552.4 985.6 553.1 982.5Q553.7 979.5 554.4 976Q555.2 972.6 556.1 969.4L568.6 924.5H597.2V1012.4H578.4V970.7Q578.4 965 578.5 959.2Q578.6 953.4 578.7 949.1Q578.9 944.7 578.9 943.4H577.9Q577.6 944.9 576.6 948.6Q575.7 952.4 574.6 956.6Q573.6 960.9 572.7 963.9L558.9 1012.4H542.3L528.5 964Q527.7 961.4 526.8 957.7Q525.9 954 525 950.1Q524.1 946.3 523.4 943.4H522.3Q522.5 946.8 522.6 951.7Q522.7 956.5 522.8 961.5Q522.9 966.5 522.9 970.7V1012.4ZM634.6 1012.4V924.5H653.8V1012.4ZM691.1 1012.4V924.5H727Q740.7 924.5 750.3 929.3Q760 934 765.1 943.7Q770.3 953.4 770.3 968.5Q770.3 983.4 765.1 993.2Q760 1002.9 750.3 1007.7Q740.7 1012.4 727 1012.4ZM710.3 996.7H726.9Q732.7 996.7 737.1 995Q741.6 993.3 744.5 990Q747.5 986.8 749.1 981.8Q750.6 976.9 750.6 970.4V966.6Q750.6 960 749.1 955.1Q747.5 950.2 744.5 946.9Q741.6 943.6 737.1 941.9Q732.7 940.2 726.9 940.2H710.3ZM803.7 1012.4V924.5H839.6Q853.3 924.5 862.9 929.3Q872.6 934 877.7 943.7Q882.9 953.4 882.9 968.5Q882.9 983.4 877.7 993.2Q872.6 1002.9 862.9 1007.7Q853.3 1012.4 839.6 1012.4ZM822.9 996.7H839.5Q845.3 996.7 849.7 995Q854.2 993.3 857.1 990Q860.1 986.8 861.6 981.8Q863.2 976.9 863.2 970.4V966.6Q863.2 960 861.6 955.1Q860.1 950.2 857.1 946.9Q854.2 943.6 849.7 941.9Q845.3 940.2 839.5 940.2H822.9ZM916.3 1012.4V924.5H935.5V996.2H979.8V1012.4ZM1010.1 1012.4V924.5H1080.2V940.2H1029.2V959.8H1074.3V975.3H1029.2V996.7H1081V1012.4ZM1158.6 1012.4V924.5H1228.7V940.2H1177.7V959.8H1222.8V975.3H1177.7V996.7H1229.5V1012.4ZM1255 1012.4 1288.7 924.5H1312.3L1346.2 1012.4H1325.5L1319.2 995.1H1281L1274.7 1012.4ZM1286.4 979.8H1313.7L1305.9 958.1Q1305.4 956.7 1304.7 954.7Q1304 952.7 1303.3 950.3Q1302.5 947.9 1301.8 945.5Q1301.1 943.1 1300.5 941.2H1299.7Q1299 943.7 1298 946.9Q1297 950.2 1296 953.2Q1295 956.2 1294.3 958.1ZM1408.8 1013.9Q1401.1 1013.9 1394.2 1012.4Q1387.3 1010.8 1382 1007.5Q1376.7 1004.1 1373.7 998.9Q1370.6 993.7 1370.6 986.3Q1370.6 985.7 1370.7 985Q1370.7 984.3 1370.7 983.9H1389.7Q1389.7 984.2 1389.6 984.8Q1389.6 985.5 1389.6 986Q1389.6 990.1 1391.8 992.9Q1394.1 995.7 1398.5 997.1Q1402.8 998.5 1408.6 998.5Q1412.2 998.5 1415.1 998.1Q1418.1 997.7 1420.3 996.9Q1422.5 996 1424 994.9Q1425.5 993.7 1426.2 992.1Q1426.9 990.6 1426.9 988.7Q1426.9 985.3 1424.6 983.1Q1422.3 980.9 1418.5 979.3Q1414.6 977.8 1409.8 976.6Q1404.9 975.3 1399.8 974Q1394.6 972.6 1389.8 970.6Q1384.9 968.7 1381 965.8Q1377.2 962.9 1374.9 958.6Q1372.6 954.3 1372.6 948.2Q1372.6 941.7 1375.4 936.9Q1378.1 932.1 1383.1 929Q1388.1 926 1394.6 924.5Q1401.2 923 1408.8 923Q1416 923 1422.4 924.5Q1428.8 926 1433.6 929.2Q1438.5 932.4 1441.3 937.2Q1444 942.1 1444.1 948.8V950.4H1425.4V949.3Q1425.4 945.9 1423.4 943.4Q1421.4 941 1417.7 939.6Q1414 938.2 1408.9 938.2Q1403.6 938.2 1399.8 939.2Q1396 940.2 1394 942.2Q1392 944.1 1392 946.9Q1392 949.9 1394.3 951.9Q1396.6 954 1400.5 955.4Q1404.3 956.8 1409.2 958Q1414 959.2 1419.2 960.6Q1424.3 962 1429.2 963.9Q1434 965.8 1437.9 968.6Q1441.8 971.5 1444 975.8Q1446.3 980 1446.3 985.9Q1446.3 996 1441.5 1002.2Q1436.6 1008.4 1428.1 1011.2Q1419.7 1013.9 1408.8 1013.9ZM1501.2 1012.4V940.7H1473V924.5H1548.7V940.7H1520.4V1012.4ZM1656.9 1013.9Q1649.2 1013.9 1642.3 1012.4Q1635.3 1010.8 1630 1007.5Q1624.7 1004.1 1621.7 998.9Q1618.7 993.7 1618.7 986.3Q1618.7 985.7 1618.7 985Q1618.8 984.3 1618.8 983.9H1637.7Q1637.7 984.2 1637.7 984.8Q1637.6 985.5 1637.6 986Q1637.6 990.1 1639.9 992.9Q1642.2 995.7 1646.5 997.1Q1650.8 998.5 1656.6 998.5Q1660.3 998.5 1663.2 998.1Q1666.1 997.7 1668.3 996.9Q1670.5 996 1672 994.9Q1673.5 993.7 1674.2 992.1Q1674.9 990.6 1674.9 988.7Q1674.9 985.3 1672.7 983.1Q1670.4 980.9 1666.5 979.3Q1662.7 977.8 1657.8 976.6Q1653 975.3 1647.8 974Q1642.7 972.6 1637.8 970.6Q1632.9 968.7 1629.1 965.8Q1625.2 962.9 1622.9 958.6Q1620.7 954.3 1620.7 948.2Q1620.7 941.7 1623.4 936.9Q1626.2 932.1 1631.1 929Q1636.1 926 1642.7 924.5Q1649.2 923 1656.8 923Q1664.1 923 1670.4 924.5Q1676.8 926 1681.7 929.2Q1686.6 932.4 1689.3 937.2Q1692.1 942.1 1692.1 948.8V950.4H1673.4V949.3Q1673.4 945.9 1671.4 943.4Q1669.4 941 1665.7 939.6Q1662 938.2 1656.9 938.2Q1651.6 938.2 1647.8 939.2Q1644 940.2 1642.1 942.2Q1640.1 944.1 1640.1 946.9Q1640.1 949.9 1642.4 951.9Q1644.6 954 1648.5 955.4Q1652.4 956.8 1657.2 958Q1662.1 959.2 1667.2 960.6Q1672.4 962 1677.2 963.9Q1682.1 965.8 1685.9 968.6Q1689.8 971.5 1692.1 975.8Q1694.4 980 1694.4 985.9Q1694.4 996 1689.5 1002.2Q1684.6 1008.4 1676.2 1011.2Q1667.7 1013.9 1656.9 1013.9ZM1727.5 1012.4V924.5H1746.7V959.5H1785.5V924.5H1804.7V1012.4H1785.5V975.6H1746.7V1012.4ZM1958.6 1012.4V940.7H1930.3V924.5H2006V940.7H1977.7V1012.4ZM2071.1 1013.9Q2063.5 1013.9 2056.5 1012.4Q2049.6 1010.8 2044.3 1007.5Q2039 1004.1 2036 998.9Q2032.9 993.7 2032.9 986.3Q2032.9 985.7 2033 985Q2033 984.3 2033.1 983.9H2052Q2052 984.2 2051.9 984.8Q2051.9 985.5 2051.9 986Q2051.9 990.1 2054.2 992.9Q2056.5 995.7 2060.8 997.1Q2065.1 998.5 2070.9 998.5Q2074.5 998.5 2077.5 998.1Q2080.4 997.7 2082.6 996.9Q2084.8 996 2086.3 994.9Q2087.8 993.7 2088.5 992.1Q2089.2 990.6 2089.2 988.7Q2089.2 985.3 2086.9 983.1Q2084.7 980.9 2080.8 979.3Q2076.9 977.8 2072.1 976.6Q2067.2 975.3 2062.1 974Q2056.9 972.6 2052.1 970.6Q2047.2 968.7 2043.4 965.8Q2039.5 962.9 2037.2 958.6Q2034.9 954.3 2034.9 948.2Q2034.9 941.7 2037.7 936.9Q2040.4 932.1 2045.4 929Q2050.4 926 2056.9 924.5Q2063.5 923 2071.1 923Q2078.3 923 2084.7 924.5Q2091.1 926 2096 929.2Q2100.8 932.4 2103.6 937.2Q2106.4 942.1 2106.4 948.8V950.4H2087.7V949.3Q2087.7 945.9 2085.7 943.4Q2083.7 941 2080 939.6Q2076.3 938.2 2071.2 938.2Q2065.9 938.2 2062.1 939.2Q2058.3 940.2 2056.3 942.2Q2054.4 944.1 2054.4 946.9Q2054.4 949.9 2056.6 951.9Q2058.9 954 2062.8 955.4Q2066.6 956.8 2071.5 958Q2076.4 959.2 2081.5 960.6Q2086.6 962 2091.5 963.9Q2096.4 965.8 2100.2 968.6Q2104.1 971.5 2106.4 975.8Q2108.6 980 2108.6 985.9Q2108.6 996 2103.8 1002.2Q2098.9 1008.4 2090.4 1011.2Q2082 1013.9 2071.1 1013.9ZM2141.8 1012.4V924.5H2161V959.5H2199.8V924.5H2219V1012.4H2199.8V975.6H2161V1012.4ZM2297.4 1013.9Q2283.4 1013.9 2273.3 1009Q2263.2 1004 2257.8 993.9Q2252.4 983.7 2252.4 968.5Q2252.4 953.1 2257.8 943Q2263.2 932.9 2273.3 928Q2283.4 923 2297.4 923Q2311.5 923 2321.6 928Q2331.6 932.9 2337 943Q2342.4 953.1 2342.4 968.5Q2342.4 983.7 2337 993.9Q2331.6 1004 2321.6 1009Q2311.5 1013.9 2297.4 1013.9ZM2297.4 998.3Q2303.6 998.3 2308.3 996.5Q2313.1 994.7 2316.3 991.1Q2319.5 987.6 2321.1 982.4Q2322.8 977.2 2322.8 970.6V966.6Q2322.8 959.8 2321.1 954.6Q2319.5 949.3 2316.3 945.8Q2313.1 942.3 2308.3 940.4Q2303.6 938.6 2297.4 938.6Q2291.2 938.6 2286.5 940.4Q2281.7 942.3 2278.5 945.8Q2275.3 949.3 2273.7 954.6Q2272.1 959.8 2272.1 966.6V970.6Q2272.1 977.2 2273.7 982.4Q2275.3 987.6 2278.5 991.1Q2281.7 994.7 2286.5 996.5Q2291.2 998.3 2297.4 998.3ZM2388.4 1012.4 2365.3 924.5H2385.5L2398.3 976.9Q2398.6 978.1 2399 980.3Q2399.4 982.5 2399.8 985Q2400.2 987.4 2400.5 989.4H2401.1Q2401.4 987.9 2401.6 986.2Q2401.8 984.4 2402.1 982.7Q2402.4 980.9 2402.7 979.4Q2403 977.9 2403.1 976.9L2415.1 924.5H2437.9L2450 976.9Q2450.3 978.2 2450.7 980.4Q2451.1 982.6 2451.5 985Q2451.9 987.5 2452.2 989.4H2452.8Q2453.1 988 2453.4 986.3Q2453.7 984.6 2454 982.9Q2454.3 981.2 2454.7 979.6Q2455.1 978.1 2455.3 976.9L2468 924.5H2486.4L2463.2 1012.4H2441.5L2428.5 958.4Q2428.1 956.6 2427.6 954.3Q2427.2 951.9 2426.8 949.6Q2426.4 947.2 2426.2 945.4H2425.7Q2425.5 947.3 2425.1 949.7Q2424.8 952.2 2424.4 954.5Q2423.9 956.7 2423.6 958.4L2410.7 1012.4Z"/>
<path fill="#c9321c" d="M1840.3 1013.9 1845 991.1H1835.4V978.3H1847.8L1851.3 961.6H1841.3V948.6H1854L1859.5 923H1872.9L1867.5 948.6H1882.6L1888 923H1901.5L1896 948.6H1906.2V961.6H1893.4L1889.9 978.3H1900.7V991.1H1887.1L1882.3 1013.9H1868.9L1873.7 991.1H1858.4L1853.7 1013.9ZM1861.1 978.3H1876.4L1880 961.6H1864.7Z"/>
<g class="ttt" fill="none" stroke="#16140f" stroke-linecap="round" stroke-linejoin="round">
<path class="gl" stroke="#c9321c" stroke-width="74" d="M 1225.3 1135.0 C 1226.9 1174.0 1234.5 1291.1 1235.1 1369.2 C 1235.7 1447.3 1229.1 1525.5 1228.9 1603.6 C 1228.8 1681.7 1232.0 1759.8 1234.1 1837.8 C 1236.3 1915.9 1241.5 1994.0 1241.8 2072.1 C 1242.1 2150.2 1235.1 2228.3 1235.9 2306.4 C 1236.7 2384.5 1245.7 2462.5 1246.5 2540.6 C 1247.2 2618.7 1241.3 2735.9 1240.3 2775.0"/>
<path class="gl" stroke="#c9321c" stroke-width="74" d="M 1758.8 1135.0 C 1760.5 1174.0 1769.6 1291.1 1769.4 1369.2 C 1769.1 1447.3 1757.2 1525.5 1757.3 1603.6 C 1757.4 1681.7 1767.0 1759.7 1770.0 1837.8 C 1773.0 1915.9 1773.7 1994.0 1775.1 2072.1 C 1776.4 2150.2 1777.7 2228.3 1778.0 2306.4 C 1778.4 2384.5 1777.4 2462.6 1777.1 2540.7 C 1776.8 2618.8 1776.3 2735.9 1776.1 2775.0"/>
<path class="gl" stroke="#c9321c" stroke-width="74" d="M 680.0 1680.5 C 719.0 1680.3 836.2 1678.2 914.3 1679.2 C 992.4 1680.2 1070.5 1687.4 1148.6 1686.6 C 1226.7 1685.7 1304.8 1675.8 1382.9 1674.0 C 1461.0 1672.2 1539.1 1674.3 1617.1 1675.9 C 1695.2 1677.4 1773.3 1681.0 1851.4 1683.2 C 1929.5 1685.4 2007.6 1689.5 2085.7 1689.1 C 2163.8 1688.7 2281.0 1682.5 2320.0 1681.1"/>
<path class="gl" stroke="#c9321c" stroke-width="74" d="M 680.0 2213.0 C 719.0 2213.7 836.2 2217.0 914.3 2217.2 C 992.4 2217.4 1070.5 2213.8 1148.6 2214.1 C 1226.7 2214.4 1304.7 2219.4 1382.9 2219.0 C 1461.0 2218.7 1539.1 2212.9 1617.2 2212.0 C 1695.3 2211.1 1773.4 2212.7 1851.5 2213.7 C 1929.6 2214.7 2007.7 2216.1 2085.8 2218.0 C 2163.8 2219.9 2281.0 2223.9 2320.0 2225.0"/>
<g class="mk" data-t="X" data-c="0"><path stroke-width="58" d="M 844.7 1292.6 C 856.6 1304.0 893.1 1337.7 916.4 1361.2 C 939.7 1384.7 961.1 1410.1 984.3 1433.7 C 1007.5 1457.3 1032.1 1479.5 1055.6 1502.8 C 1079.2 1526.1 1113.8 1561.6 1125.5 1573.4"/><path stroke-width="58" d="M 1125.5 1285.6 C 1114.3 1297.8 1082.0 1335.6 1058.7 1359.2 C 1035.5 1382.7 1009.4 1403.6 986.0 1427.0 C 962.6 1450.5 940.8 1475.4 918.4 1499.8 C 896.0 1524.2 862.8 1561.1 851.7 1573.4"/></g>
<g class="mk" data-t="O" data-c="1"><path stroke-width="56" d="M 1345.6 1412.6 C 1350.5 1401.3 1360.5 1363.2 1374.9 1345.0 C 1389.2 1326.8 1411.1 1313.9 1431.8 1303.2 C 1452.5 1292.4 1477.2 1279.4 1499.1 1280.6 C 1521.0 1281.9 1544.1 1297.9 1563.3 1310.7 C 1582.6 1323.5 1601.9 1338.4 1614.7 1357.5 C 1627.5 1376.7 1639.0 1402.6 1640.2 1425.5 C 1641.4 1448.5 1631.4 1474.0 1621.9 1495.5 C 1612.5 1517.0 1600.5 1540.5 1583.4 1554.6 C 1566.3 1568.6 1541.6 1574.9 1519.4 1579.7 C 1497.3 1584.5 1472.2 1588.9 1450.5 1583.4 C 1428.8 1577.9 1405.2 1563.5 1389.1 1546.8 C 1373.1 1530.1 1361.3 1506.0 1354.1 1483.1 C 1346.9 1460.3 1341.7 1432.7 1346.0 1409.8 C 1350.4 1387.0 1365.9 1364.6 1380.3 1345.9 C 1394.6 1327.2 1423.5 1305.6 1432.2 1297.6"/></g>
<g class="mk" data-t="X" data-c="2"><path stroke-width="58" d="M 1882.2 1296.8 C 1894.0 1308.5 1929.9 1342.8 1952.8 1366.7 C 1975.7 1390.5 1997.0 1416.0 2019.8 1440.1 C 2042.5 1464.2 2065.2 1488.3 2089.1 1511.2 C 2112.9 1534.1 2150.7 1566.6 2163.0 1577.6"/><path stroke-width="58" d="M 2163.0 1289.8 C 2151.7 1301.8 2117.9 1338.2 2094.8 1362.0 C 2071.8 1385.7 2046.9 1407.8 2024.7 1432.4 C 2002.5 1456.9 1984.1 1485.1 1961.5 1509.3 C 1939.0 1533.5 1901.3 1566.3 1889.3 1577.6"/></g>
<g class="mk" data-t="X" data-c="3"><path stroke-width="58" d="M 845.2 1818.7 C 856.2 1831.1 888.3 1869.2 911.3 1893.1 C 934.2 1917.0 958.5 1939.5 982.9 1961.9 C 1007.3 1984.2 1033.8 2004.5 1057.7 2027.5 C 1081.5 2050.4 1114.6 2087.5 1126.0 2099.5"/><path stroke-width="58" d="M 1126.0 1811.7 C 1114.8 1823.8 1081.6 1860.8 1058.6 1884.6 C 1035.5 1908.4 1010.0 1929.7 987.7 1954.2 C 965.4 1978.7 947.4 2007.3 924.9 2031.5 C 902.3 2055.7 864.4 2088.2 852.3 2099.5"/></g>
<g class="mk" data-t="O" data-c="4"><path stroke-width="56" d="M 1602.5 2043.3 C 1593.7 2051.2 1570.1 2082.5 1549.7 2090.4 C 1529.3 2098.3 1502.3 2094.8 1480.1 2090.7 C 1457.9 2086.7 1434.8 2079.0 1416.6 2066.0 C 1398.4 2053.1 1381.9 2033.5 1370.8 2013.0 C 1359.7 1992.5 1349.0 1965.9 1349.9 1943.0 C 1350.8 1920.1 1364.7 1896.0 1376.3 1875.5 C 1387.9 1855.1 1401.4 1833.8 1419.5 1820.4 C 1437.5 1807.0 1462.0 1798.2 1484.6 1795.1 C 1507.2 1791.9 1533.7 1793.2 1555.1 1801.7 C 1576.4 1810.2 1597.5 1827.6 1612.4 1846.0 C 1627.3 1864.5 1640.5 1889.3 1644.5 1912.5 C 1648.4 1935.7 1642.8 1962.5 1636.0 1985.1 C 1629.2 2007.7 1618.8 2031.3 1603.8 2048.2 C 1588.7 2065.1 1566.9 2078.1 1545.6 2086.6 C 1524.4 2095.2 1487.8 2097.2 1476.3 2099.3"/></g>
<g class="mk" data-t="O" data-c="5"><path stroke-width="56" d="M 2096.2 2077.2 C 2085.1 2079.8 2052.1 2092.8 2029.8 2092.8 C 2007.6 2092.7 1982.6 2087.7 1962.8 2077.0 C 1942.9 2066.3 1922.8 2048.1 1910.9 2028.5 C 1899.0 2008.9 1892.6 1982.4 1891.4 1959.3 C 1890.3 1936.3 1896.0 1911.8 1904.2 1890.3 C 1912.4 1868.8 1924.1 1845.5 1940.5 1830.3 C 1956.9 1815.2 1980.7 1804.5 2002.6 1799.4 C 2024.6 1794.2 2049.8 1793.8 2072.0 1799.2 C 2094.2 1804.6 2119.2 1815.6 2135.9 1831.7 C 2152.5 1847.7 2165.2 1872.9 2171.9 1895.8 C 2178.6 1918.6 2179.7 1945.2 2176.0 1968.6 C 2172.4 1992.1 2163.3 2017.1 2150.0 2036.2 C 2136.7 2055.3 2116.5 2074.2 2096.1 2083.2 C 2075.7 2092.3 2050.4 2091.1 2027.4 2090.5 C 2004.4 2090.0 1969.6 2081.7 1958.1 2080.0"/></g>
<g class="mk" data-t="O" data-c="6"><path stroke-width="56" d="M 1030.4 2342.7 C 1040.2 2348.9 1074.2 2363.1 1089.4 2379.7 C 1104.6 2396.3 1114.7 2420.0 1121.7 2442.3 C 1128.8 2464.5 1135.7 2491.1 1131.7 2513.4 C 1127.6 2535.6 1112.0 2558.2 1097.4 2575.7 C 1082.9 2593.2 1064.3 2609.1 1044.5 2618.6 C 1024.6 2628.0 1000.1 2633.8 978.3 2632.4 C 956.6 2631.1 932.9 2622.3 914.0 2610.4 C 895.0 2598.5 875.7 2580.9 864.8 2561.3 C 853.9 2541.6 848.9 2515.5 848.4 2492.6 C 847.9 2469.8 852.8 2445.1 861.8 2424.4 C 870.9 2403.8 885.5 2382.7 902.6 2368.8 C 919.7 2355.0 943.0 2345.7 964.7 2341.5 C 986.4 2337.3 1012.1 2336.6 1032.7 2343.8 C 1053.3 2351.1 1071.7 2368.4 1088.1 2384.8 C 1104.5 2401.2 1123.9 2432.6 1131.0 2442.2"/></g>
<g class="mk" data-t="X" data-c="7"><path stroke-width="58" d="M 1362.8 2324.8 C 1374.8 2336.1 1411.5 2369.7 1435.1 2392.9 C 1458.7 2416.0 1481.2 2440.3 1504.5 2463.9 C 1527.7 2487.4 1551.4 2510.6 1574.5 2534.2 C 1597.7 2557.8 1632.1 2593.7 1643.6 2605.6"/><path stroke-width="58" d="M 1643.6 2317.7 C 1632.7 2330.3 1601.7 2369.3 1578.4 2392.8 C 1555.1 2416.3 1527.7 2436.0 1503.7 2458.8 C 1479.6 2481.5 1456.3 2505.1 1434.0 2529.5 C 1411.7 2554.0 1380.5 2592.9 1369.8 2605.6"/></g>
<g class="mk" data-t="X" data-c="8"><path stroke-width="58" d="M 1878.6 2334.1 C 1890.9 2345.3 1928.4 2378.0 1952.4 2400.8 C 1976.3 2423.7 1999.4 2447.4 2022.4 2471.2 C 2045.5 2494.9 2067.7 2519.4 2090.6 2543.4 C 2113.4 2567.4 2148.0 2603.0 2159.4 2614.9"/><path stroke-width="58" d="M 2159.4 2327.1 C 2148.4 2339.4 2116.3 2377.5 2093.0 2401.0 C 2069.8 2424.6 2042.4 2444.2 2019.7 2468.3 C 1997.0 2492.4 1979.4 2521.4 1957.1 2545.8 C 1934.7 2570.2 1897.6 2603.4 1885.7 2614.9"/></g>
</g></svg>"""

COVER_JS = """(function () {
  var svg = document.querySelector('.cover-live');
  if (!svg || !svg.animate) return;
  if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var COL = 519, ROW = 520;      // distance between squares, in the cover's 3000-unit space
  var FIRST = 2600, HOLD = 3400; // how long a finished board stays up (ms): on arrival, and after each game
  var GAMES = [                  // finished board (top row first) and the order the moves are played in
    { board: 'XOXXXOOXO', order: [0, 6, 4, 8, 7, 1, 3, 5, 2] },
    { board: 'OXOXOXXOX', order: [3, 7, 5, 4, 1, 2, 6, 0, 8] },
    { board: 'XOXXOOOXX', order: [0, 4, 8, 1, 7, 6, 2, 5, 3] }   // the cover's own game
  ];
  var board = svg.querySelector('.ttt');
  var lines = [].slice.call(board.querySelectorAll('.gl'));
  var marks = [].slice.call(board.querySelectorAll('.mk'));
  var strokes = [].slice.call(board.querySelectorAll('path'));
  var onScreen = true;
  if ('IntersectionObserver' in window) new IntersectionObserver(function (e) { onScreen = e[0].isIntersecting; }).observe(svg);
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function hide(p) { p.style.strokeDasharray = p.len + ' ' + (p.len + 300); p.style.strokeDashoffset = p.len; p.style.visibility = 'hidden'; }
  function draw(p, ms) {
    p.style.visibility = 'visible';
    return p.animate([{ strokeDashoffset: p.len }, { strokeDashoffset: 0 }], { duration: ms, easing: 'cubic-bezier(.4,0,.3,1)' })
      .finished.then(function () { p.style.strokeDasharray = ''; p.style.strokeDashoffset = ''; });
  }
  // move the drawing's 5 X's and 4 O's into the squares of the next game
  function place(game) {
    var pool = { X: [], O: [] }, at = [];
    marks.forEach(function (m) { pool[m.getAttribute('data-t')].push(m); });
    game.board.split('').forEach(function (t, sq) {
      var m = pool[t].shift(), home = +m.getAttribute('data-c');
      m.setAttribute('transform', 'translate(' + ((sq % 3) - (home % 3)) * COL + ' ' + (Math.floor(sq / 3) - Math.floor(home / 3)) * ROW + ')');
      at[sq] = m;
    });
    return at;
  }
  async function loop() {
    strokes.forEach(function (p) { p.len = Math.ceil(p.getTotalLength()) + 2; });
    for (var n = 0; ; n++) {
      await wait(n ? HOLD : FIRST);
      while (document.hidden || !onScreen) await wait(500);
      // wipe the whole board, red lines included
      var fade = board.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 450, easing: 'ease-in', fill: 'forwards' });
      await fade.finished;
      strokes.forEach(hide);
      var at = place(GAMES[n % GAMES.length]);
      fade.cancel();
      await wait(350);
      // draw the red lines from scratch
      for (var i = 0; i < lines.length; i++) { await draw(lines[i], 330); await wait(70); }
      await wait(150);
      // play the game, X first
      for (var k = 0; k < 9; k++) {
        var ps = at[GAMES[n % GAMES.length].order[k]].querySelectorAll('path');
        for (var j = 0; j < ps.length; j++) { await draw(ps[j], ps.length === 2 ? 140 : 300); if (ps.length === 2 && j === 0) await wait(60); }
        await wait(210);
      }
    }
  }
  loop().catch(function () {});
})();"""


def cover_html(show):
    if ANIMATED_COVER and ANIMATED_COVER in (show["image"] or ""):
        return COVER_SVG + "\n<script>\n" + COVER_JS + "\n</script>"
    return f'<img class="cover" src="{esc(show["image"])}" alt="MESS podcast cover art" width="360" height="360">'


def home(show, eps):
    latest = eps[0]
    by_num = {e["num"]: e for e in eps}
    picks = [by_num[n] for n in CFG.get("start_here", []) if n in by_num]
    picks_html = ""
    if picks:
        note = f'<p class="lede-sm">{esc(CFG["start_here_note"])}</p>' if CFG.get("start_here_note") else ""
        picks_html = f'<section class="band"><div class="wrap"><h2 class="kicker">Start here</h2>{note}<ul class="cards">' + "".join(
            f'<li><a href="{p["url"]}"><span class="ep-num">{esc(ep_label(p))}</span><strong>{esc(p["title"])}</strong><span>{esc(p["summary"])}</span></a></li>'
            for p in picks) + "</ul></div></section>"
    hosts = "".join(host_html(h, "h3") for h in CFG["hosts"])
    body = f"""
<main>
<section class="hero">
  <div class="wrap hero-grid">
    <div>
      <h1 class="masthead">Middle East<br><span>Shitshow</span></h1>
      <p class="tagline">{esc(CFG['tagline'])}</p>
      {listen_links()}
    </div>
    {cover_html(show)}
  </div>
</section>

<section class="latest">
  <div class="wrap">
    <p class="kicker kicker-red">Latest episode &middot; {latest['date_str']}</p>
    <h2 class="latest-title"><a href="{latest['url']}">{esc(latest['title'])}</a></h2>
    <p class="latest-sum">{esc(plain(latest['html'], 420))}</p>
    {player(latest)}
    <p><a class="more" href="{latest['url']}">Full show notes</a></p>
  </div>
</section>

<section class="band band-hosts">
  <div class="wrap hosts">{hosts}</div>
</section>

{picks_html}

<section class="band">
  <div class="wrap">
    <h2 class="kicker">Recent episodes</h2>
    <ul class="ep-list">{''.join(ep_row(e) for e in eps[1:7])}</ul>
    <p><a class="more" href="/episodes/">All {len(eps)} episodes</a></p>
  </div>
</section>
</main>"""
    ld = {"@context": "https://schema.org", "@type": "PodcastSeries", "name": CFG["title"],
          "url": BASE_URL + "/", "image": show["image"], "description": CFG["meta_description"],
          "webFeed": CFG["feed_url"], "author": [{"@type": "Person", "name": h["name"]} for h in CFG["hosts"]]}
    return page(CFG["title"], body, CFG["meta_description"], "/", show["image"],
                f'<script type="application/ld+json">{json.dumps(ld)}</script>')


def archive(show, eps):
    body = f"""<main class="wrap narrow">
<h1 class="page-title">Episodes</h1>
<p class="lede-sm">{len(eps)} episodes, newest first.</p>
<ul class="ep-list">{''.join(ep_row(e) for e in eps)}</ul>
</main>"""
    return page("Episodes", body, f"All episodes of {CFG['title']}.", "/episodes/", show["image"])


def episode(show, eps, i):
    ep = eps[i]
    newer = eps[i - 1] if i > 0 else None
    older = eps[i + 1] if i + 1 < len(eps) else None
    nav = '<nav class="ep-nav">' + (
        f'<a href="{older["url"]}"><small>Previous</small>{esc(older["title"])}</a>' if older else "<span></span>") + (
        f'<a class="next" href="{newer["url"]}"><small>Next</small>{esc(newer["title"])}</a>' if newer else "<span></span>") + "</nav>"
    body = f"""<main class="wrap narrow">
<article class="episode">
  <p class="kicker kicker-red">{esc(ep_label(ep))} &middot; {ep['date_str']} &middot; {ep['duration']}</p>
  <h1 class="page-title">{esc(ep['title'])}</h1>
  {player(ep)}
  <div class="notes">{ep['html']}</div>
  {ep_videos(ep)}
  <div class="ep-listen"><p class="kicker">Listen on</p>{listen_links()}</div>
</article>
{nav}
</main>"""
    ld = {"@context": "https://schema.org", "@type": "PodcastEpisode", "name": ep["title"],
          "url": BASE_URL + ep["url"], "datePublished": ep["iso"], "description": ep["meta"],
          "episodeNumber": ep["num"], "associatedMedia": {"@type": "MediaObject", "contentUrl": ep["audio"]},
          "partOfSeries": {"@type": "PodcastSeries", "name": CFG["title"], "url": BASE_URL + "/"}}
    return page(ep["title"], body, ep["meta"], ep["url"], show["image"],
                f'<script type="application/ld+json">{json.dumps(ld)}</script>')


def contact_page(show):
    c = CFG["contact"]
    topics = "".join(f"<option>{esc(t)}</option>" for t in c.get("topics", []))
    topic_field = f"""<label>Topic<select name="topic">{topics}</select></label>""" if topics else ""
    note = f'<p class="lede-sm">{esc(c["privacy_note"])}</p>' if c.get("privacy_note") else ""
    body = f"""<main class="wrap narrow">
<h1 class="page-title">Contact</h1>
<p class="lede">{esc(c.get("intro", ""))}</p>
{note}
<form class="contact" id="contact-form" action="https://formspree.io/f/{esc(c['formspree_id'])}" method="POST">
  <input type="hidden" name="_subject" value="New message from the MESS website">
  <input type="text" name="_gotcha" class="hp" tabindex="-1" autocomplete="off" aria-hidden="true">
  <label>Your name<input type="text" name="name" required autocomplete="name"></label>
  <label>Your email<input type="email" name="email" required autocomplete="email"></label>
  {topic_field}
  <label>Message<textarea name="message" rows="7" required></textarea></label>
  <button type="submit">Send</button>
  <p class="form-status" id="form-status" role="status" aria-live="polite"></p>
</form>
</main>
<script>
(function () {{
  var f = document.getElementById("contact-form"), s = document.getElementById("form-status");
  f.addEventListener("submit", function (e) {{
    e.preventDefault();
    var b = f.querySelector("button");
    b.disabled = true; s.className = "form-status"; s.textContent = "Sending...";
    var data = Object.fromEntries(new FormData(f));
    data._subject = "MESS website: " + (data.topic || "message") + " from " + data.name;
    fetch(f.action, {{ method: "POST", headers: {{ "Content-Type": "application/json", Accept: "application/json" }}, body: JSON.stringify(data) }})
      .then(function (r) {{
        return r.json().catch(function () {{ return {{}}; }}).then(function (j) {{
          if (r.ok) {{ f.reset(); s.className = "form-status ok"; s.textContent = "Thanks. Your message is on its way."; return; }}
          var msg = (j.errors || []).map(function (x) {{ return x.message; }}).join(" ");
          throw new Error(msg || "failed");
        }});
      }})
      .catch(function (err) {{
        s.className = "form-status err";
        s.textContent = (err && err.message && err.message !== "failed" && err.message !== "Failed to fetch")
          ? "Your message wasn't sent: " + err.message
          : "Something went wrong and your message wasn't sent. Please try again in a minute.";
      }})
      .finally(function () {{ b.disabled = false; }});
  }});
}})();
</script>"""
    return page("Contact", body, f"Contact {CFG['title']}.", "/contact/", show["image"])


def not_found(show):
    body = '<main class="wrap narrow"><h1 class="page-title">Not here.</h1><p class="lede">That page doesn\'t exist. Try the <a href="/episodes/">episode list</a>.</p></main>'
    return page("Page not found", body, CFG["meta_description"], "/404.html", show["image"])


# ---------- build ----------

LIST_START, LIST_END = "<!-- episode-list:start", "<!-- episode-list:end -->"


def fill_episode_lists(eps):
    """Refills the episode list in static pages (used by webby.html), newest first, as on the Episodes page.
    The start marker can name a season: <!-- episode-list:start season=1 --> lists only that season
    (episodes without a season tag are included). A page without both markers is left as it is."""
    for p in sorted(OUT.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        a = text.find(LIST_START)
        b = text.find(LIST_END, a + 1) if a != -1 else -1
        if a == -1 or b == -1:
            continue
        head_end = text.index("-->", a) + 3
        m = re.search(r"season=(\d+)", text[a:head_end])
        season = int(m.group(1)) if m else None
        rows = "".join(
            f'      <li class="ep-row"><a href="{ep["url"]}"><span class="ep-num">{esc(ep_label(ep))}</span>'
            f'<span class="ep-title">{esc(ep["title"])}</span>'
            f'<span class="ep-meta">{ep["date_short"]} &middot; {ep["duration"]}</span></a></li>\n'
            for ep in eps if season is None or ep["season"] in (None, season))
        p.write_text(text[:head_end] + "\n" + soften(rows) + text[b:], encoding="utf-8")


COVER_URL = re.compile(r"https://media\.rss\.com/[\w-]+/podcast_cover_\w+\.(?:png|jpe?g|webp)")


def fill_show_cover(show):
    """Points static pages (webby.html) at the feed's current show cover, so a new cover uploaded
    in RSS.com reaches them on the next build. Only RSS.com show-cover addresses are changed."""
    if not show["image"]:
        return
    for p in sorted(OUT.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        new = COVER_URL.sub(lambda m: esc(show["image"]), text)
        if new != text:
            p.write_text(new, encoding="utf-8")


def soften(text):
    """Display spelling of the show name. URLs (lowercase, hyphenated) are left alone."""
    text = text.replace("Shitshow", "Sh#tshow").replace("SHITSHOW", "SH#TSHOW")
    return re.sub(r"(?<![\w./-])shitshow(?![\w.-])", "sh#tshow", text)


def link_targets(text):
    """Open links in a new tab: external links only, or all links, per site.json."""
    mode = CFG.get("open_links_in_new_tab", "external")
    def fix(m):
        tag, href = m.group(0), m.group(1)
        if "target=" in tag:
            return tag
        external = href.startswith(("http://", "https://")) and CFG["domain"] not in href
        if external or (mode == "all" and not href.startswith(("mailto:", "#"))):
            tag = tag[:-1] + ' target="_blank"' + ("" if "rel=" in tag else ' rel="noopener"') + ">"
        return tag
    return re.sub(r'<a\s[^>]*href="([^"]*)"[^>]*>', fix, text)


def write(rel, text):
    if rel.endswith(".html"):
        text = link_targets(soften(text))
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", help="build from a local feed file instead of fetching")
    args = ap.parse_args()
    show, eps = load_feed(args.feed)
    if not eps:
        sys.exit("Feed has no episodes; refusing to publish an empty site.")
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ROOT / "static", OUT)
    fill_episode_lists(eps)
    fill_show_cover(show)
    write("index.html", home(show, eps))
    write("episodes/index.html", archive(show, eps))
    for i, ep in enumerate(eps):
        write(f"episodes/{ep['slug']}/index.html", episode(show, eps, i))
    write("404.html", not_found(show))
    if contact_on():
        write("contact/index.html", contact_page(show))
    write("CNAME", CFG["domain"] + "\n")
    urls = ["/", "/episodes/"] + (["/contact/"] if contact_on() else []) + [e["url"] for e in eps]
    write("sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          + "".join(f"<url><loc>{BASE_URL}{u}</loc></url>" for u in urls) + "</urlset>\n")
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n")
    print(f"Built {len(eps)} episodes into {OUT}")


if __name__ == "__main__":
    main()
