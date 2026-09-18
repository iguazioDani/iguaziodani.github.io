#!/usr/bin/env python3
"""Builds the MESS website from the RSS.com feed.

Usage:
  python3 build.py                 # fetch the live feed, write the site to _site/
  python3 build.py --feed FILE     # build from a local copy of the feed

Standard library only, so GitHub Actions needs nothing installed.
Edit site.json for copy and links; edit static/style.css for the look.
"""
import argparse, email.utils, html, json, re, shutil, sys, urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "_site"
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
CFG = json.loads((ROOT / "site.json").read_text())
BASE_URL = f"https://{CFG['domain']}"
esc = html.escape


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
        date = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
        title = it.findtext("title", "").strip()
        desc = it.findtext("description", "")
        slug = f"{num}-{slugify(title)}" if num else slugify(title) or f"episode-{len(items) - i}"
        eps.append({
            "num": num, "type": ep_type, "title": title, "date": date,
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
<link rel="icon" href="{esc(image)}">
<link rel="alternate" type="application/rss+xml" title="{esc(CFG['title'])}" href="{esc(CFG['feed_url'])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&family=Source+Serif+4:ital,opsz,wght@0,8..60,400..700;1,8..60,400..700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css">
{extra_head}
</head>
<body>
<header class="topbar">
  <a class="wordmark" href="/">MESS<span>Middle East Shitshow</span></a>
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


def player(ep):
    return f'<audio controls preload="none" src="{esc(ep["audio"])}"></audio>' if ep["audio"] else ""


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
    bio = "".join("<p>" + re.sub(r"\*([^*]+)\*", r"<em>\1</em>", esc(x)) + "</p>" for x in paras)
    link = ""
    if h.get("link"):
        link = f'<p class="host-link"><a href="{esc(h["link"]["url"])}" rel="noopener">{esc(h["link"]["label"])}</a></p>'
    return f'<div class="host"><p class="kicker">{esc(h["role"])}</p><{tag}>{esc(h["name"])}</{tag}>{bio}{link}</div>'


def contact_on():
    return bool(CFG.get("contact", {}).get("formspree_id"))


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
    <img class="cover" src="{esc(show['image'])}" alt="MESS podcast cover art" width="360" height="360">
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


def about(show):
    hosts = "".join(host_html(h, "h2") for h in CFG["hosts"])
    contact = ""
    if contact_on():
        contact = '<h2 class="kicker kicker-gap">Get in touch</h2><p><a class="more" href="/contact/">Send us a message</a></p>'
    body = f"""<main class="wrap narrow">
<h1 class="page-title">About the show</h1>
<p class="lede">{esc(show['description'])}</p>
<div class="hosts hosts-stack">{hosts}</div>
<h2 class="kicker">Listen</h2>
{listen_links()}
{contact}
</main>"""
    return page("About", body, CFG["meta_description"], "/about/", show["image"])


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
