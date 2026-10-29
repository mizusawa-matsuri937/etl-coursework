"""任务 3 合成产品页（spec AC03、AC04）：仿照 amazon.com 经典模板的最小页面，非真实商品。

每页都带一段含 “DVD Blu-ray Prime Video Movies & TV” 的导航、一段含 Television / Season / Double Feature 的
商品描述，用来证明这些区域的文字不会被当作依据。CASES 的预期值按 spec AC04 手写，不由实现生成。
"""

from __future__ import annotations

from html import escape

NAV = '<div id="nav-main"><a>DVD</a> <a>Blu-ray</a> <a>Prime Video</a> <a>Movies &amp; TV</a> <a>VHS</a></div>'
DESCRIPTION = ('<div id="productDescription"><p>A Television event! Season 1 Double Feature, Live in Concert, '
               'Director Jane Roe, Run time 1 hour and 50 minutes.</p></div>')


def page(title: str | None, crumbs: tuple[str, ...] = (), bullets: tuple[tuple[str, str], ...] = (),
         overview: tuple[tuple[str, str], ...] = (), byline: tuple[tuple[str, str], ...] = (),
         rated: str | None = None, fmt: str | None = None, swatch: str | None = None,
         overview_script: bool = False) -> bytes:
    parts = ["<!doctype html><html><head><title>Amazon.com</title>",
             "<script>var productType = 'PHYSICAL_TV_SERIES';</script></head><body>", NAV]
    if crumbs:
        items = '<li class="a-breadcrumb-divider"><span class="a-list-item">›</span></li>'.join(
            f'<li><span class="a-list-item"><a class="a-link-normal">{escape(c)}</a></span></li>' for c in crumbs)
        parts.append(f'<div id="wayfinding-breadcrumbs_feature_div"><ul>{items}</ul></div>')
    if title is not None:
        parts.append(f'<h1><span id="productTitle" class="a-size-large">  {escape(title)}  </span></h1>')
    if byline or rated or fmt:
        b = ['<div id="bylineInfo" class="a-section">']
        for name, role in byline:
            b.append(f'<span class="author"><a class="a-link-normal">{escape(name)}</a>'
                     f'<span class="contribution"><span class="a-color-secondary">({escape(role)}), </span>'
                     f'</span></span>')
        if rated:
            b.append(f'<i class="a-icon"></i><span class="a-color-secondary">Rated: </span>'
                     f'<div class="a-box"><div class="a-box-inner"><span class="a-size-small">{escape(rated)}</span>'
                     f'</div></div>')
        if fmt:
            b.append(f'<i class="a-icon"></i><span class="a-color-secondary">Format: </span><span>{escape(fmt)}</span>')
        parts.append("".join(b) + "</div>")
    if swatch:
        parts.append('<div id="tmmSwatches"><ul><li class="swatchElement unselected"><a><span aria-label="Prime Video ">'
                     'Prime Video</span></a></li><li class="swatchElement selected"><a class="a-button-text">'
                     f'<span aria-label="{escape(swatch)} ">{escape(swatch)}</span><br/>'
                     '<span aria-label="$9.99">$9.99</span></a></li></ul></div>')
    if overview:
        rows = []
        for i, (k, v) in enumerate(overview):
            script = "<script>(function(f){var po = 'Genre: Television';})()</script>" if overview_script and i == 0 \
                else ""
            rows.append(f'<tr><td class="a-span3"><span class="a-text-bold">{escape(k)}</span></td>'
                        f'<td class="a-span9">{script}<span class="po-break-word">{escape(v)}</span></td></tr>')
        parts.append(f'<div id="productOverview_feature_div"><table>{"".join(rows)}</table></div>')
    if bullets:
        lis = "".join(f'<li><span class="a-list-item"><span class="a-text-bold">{escape(k)} &rlm; : &lrm; </span>'
                      f'<span>{escape(v)}</span></span></li>' for k, v in bullets)
        parts.append(f'<div id="detailBullets_feature_div"><div id="detailBullets_feature_div"><ul>{lis}</ul>'
                     '</div></div>')
    parts += [DESCRIPTION, "</body></html>"]
    return "".join(parts).encode("utf-8")


VIDEO = ("Movies & TV", "Featured Categories", "DVD")
FEATURE = (("Director", "Jane Roe"), ("Actors", "Ann Lee, Bob Ray"), ("Run time", "1 hour and 50 minutes"))

# AC03：导航与描述含载体字样，面包屑为 CDs & Vinyl，概览 Genre 单元格含脚本，要点含未知标签与 Run time
EXTRACT_PAGE = page("Night Songs", crumbs=("CDs & Vinyl", "Rock"),
                    bullets=(("Item model number", "MX-1"), ("Run time", "45 minutes"), ("Dubbed:", "French")),
                    overview=(("Genre", "Rock"), ("Runtime", "45 minutes")), overview_script=True,
                    byline=(("Ann Lee", "Actor, Director"),), fmt="Audio CD")
EXTRACT_EXPECTED = [  # (区域, 序号, 标签, 原值, 字段键)
    ("title", 1, None, "Night Songs", "title"),
    ("breadcrumb", 1, None, "CDs & Vinyl", "breadcrumb"),
    ("breadcrumb", 2, None, "Rock", "breadcrumb"),
    ("detail_bullets", 1, "Item model number", "MX-1", None),
    ("detail_bullets", 2, "Run time", "45 minutes", "runtime"),
    ("detail_bullets", 3, "Dubbed", "French", "dubbed"),
    ("product_overview", 1, "Genre", "Rock", "genre"),
    ("product_overview", 2, "Runtime", "45 minutes", "runtime"),
    ("byline", 1, "Actor", "Ann Lee", "actors"),
    ("byline", 2, "Director", "Ann Lee", "director"),
    ("byline", 3, "Format", "Audio CD", "media_format"),
]

# AC04：(名称, 页面, 类别, 证据级别, rule_id, 待确认原因)
CASES = [
    ("structured_movie", page("Quiet River", crumbs=VIDEO, bullets=FEATURE + (("Media Format", "NTSC"),)),
     "movie", "structured", "MV1", None),
    ("inferred_movie", page("Hill Road", crumbs=VIDEO, bullets=(("Actors", "Ann Lee"), ("Run time", "95 minutes"))),
     "movie", "inferred", "MV2", None),
    ("title_only_dvd", page("Old Harbor [DVD]", crumbs=("Movies & TV",)),
     "pending", "missing", "PD1", "insufficient_evidence"),
    ("tv_genre", page("City Lights", crumbs=VIDEO, overview=(("Genre", "Drama, Television"),)),
     "tv_series", "structured", "TV1", None),
    ("season_title", page("City Lights: Season 3", crumbs=VIDEO, bullets=(("Media Format", "DVD"),)),
     "tv_series", "inferred", "TV2", None),
    ("concert_genre", page("Arena Night", crumbs=VIDEO, overview=(("Genre", "Music Videos & Concerts"),)),
     "concert_other", "structured", "CO1", None),
    ("workout_title", page("Total Body Workout", crumbs=VIDEO, fmt="DVD"),
     "concert_other", "inferred", "CO2", None),
    ("double_feature", page("Red Sky / Blue Sea (Double Feature)", crumbs=VIDEO, fmt="DVD"),
     "collection", "inferred", "CL2", None),
    ("criterion_single", page("Seven Rivers (The Criterion Collection)", crumbs=VIDEO, bullets=FEATURE),
     "movie", "structured", "MV1", None),
    ("box_set_single", page("Long Voyage", crumbs=VIDEO,
                            bullets=FEATURE + (("Media Format", "Box set, NTSC"), ("Number of discs", "4"))),
     "movie", "structured", "MV1", None),
    ("cd_no_video", page("Night Songs", crumbs=("CDs & Vinyl", "Rock"), fmt="Audio CD"),
     "non_movie", "structured", "NM1", None),
    ("cd_root_vhs_movie", page("Panic Street [VHS]", crumbs=("CDs & Vinyl", "Pop"),
                               bullets=FEATURE, fmt="VHS Tape"),
     "movie", "structured", "MV1", None),
    ("no_breadcrumb_no_carrier", page("Plain Item", bullets=(("Studio", "Acme"),)),
     "pending", "missing", "PX2", "no_video_evidence"),
    ("tv_genre_with_film_signals", page("The Tenth Hour", crumbs=VIDEO, bullets=FEATURE,
                                        overview=(("Genre", "Drama, Television"),)),
     "tv_series", "structured", "TV1", None),
    ("electronics_season_no_carrier", page("Ocean Days: Season 2", crumbs=("Electronics", "Accessories")),
     "pending", "missing", "PX1", "conflicting_evidence"),
    # 以下 3 例是 t3r2 修复的已知问题（t3r1 依次判为 CL2、MV2、NM1）
    ("carrier_list_title", page("Happy Paws (Blu-ray 3D / Blu-ray / DVD)", crumbs=VIDEO, bullets=FEATURE),
     "movie", "structured", "MV1", None),
    ("placeholder_actors", page("Garden Tales", crumbs=VIDEO,
                                bullets=(("Actors", "Artist Not Provided"), ("Run time", "95 minutes"))),
     "pending", "missing", "PD1", "insufficient_evidence"),
    ("cd_root_dvd_title", page("Island Band: Live in Europe 2003 [DVD]", crumbs=("CDs & Vinyl", "Reggae")),
     "pending", "missing", "PX1", "conflicting_evidence"),
]
