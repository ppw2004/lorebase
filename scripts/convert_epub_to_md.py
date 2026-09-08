#!/usr/bin/env python3
"""epub → markdown 转换(通用), 产出后续管线可用的章(# )/节(## )结构。

仅用标准库。针对 calibre 等工具产出的 HTML 脏结构(<b> 等行内标签跨元素嵌套),
html.parser 事件流下按块边界重置状态即可正确处理。

用法:
  python3 convert_epub_to_md.py book.epub corpus/book.md
"""
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

HEAD_MAP = {"h1": "#", "h2": "#", "h3": "##", "h4": "##"}  # h1/h2→章, h3/h4→节
NS_OPF = "{http://www.idpf.org/2007/opf}"


def safe_xml(data):
    """禁 DTD 实体声明(epub 来自不可信来源, 防 entity expansion)。"""
    p = ElementTree.XMLParser()
    p.parser.EntityDeclHandler = lambda *a: (_ for _ in ()).throw(
        ValueError("DTD entities disabled"))
    return ElementTree.fromstring(data, parser=p)


def read_spine(zf):
    """从 content.opf 解析 spine 顺序, 返回 [href]。"""
    opf_name = next(n for n in zf.namelist() if n.endswith(".opf"))
    opf = safe_xml(zf.read(opf_name))
    base = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""
    manifest = {i.get("id"): (i.get("href"), i.get("media-type"))
                for i in opf.iter(f"{NS_OPF}manifest/{NS_OPF}item")}
    out = []
    for ref in opf.iter(f"{NS_OPF}spine/{NS_OPF}itemref"):
        item = manifest.get(ref.get("idref"))
        if item and (item[1] or "").endswith("html"):
            href = item[0]
            if not zf.exists(base + href) and zf.exists(href):
                href_q, base = href, ""
            else:
                href_q = base + href
            if zf.exists(href_q):
                out.append(href_q)
    return out


class BlockParser(HTMLParser):
    BLOCK_TAGS = {"h1", "h2", "h3", "h4", "p", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._tag = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS and self._tag is None:
            self._tag, self._buf = tag, []

    def handle_endtag(self, tag):
        if tag == self._tag:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.blocks.append((self._tag, text))
            self._tag = None

    def handle_data(self, data):
        if self._tag:
            self._buf.append(data)


def convert(epub_path, out_path):
    zf = zipfile.ZipFile(epub_path)
    out = []
    seen_titles = set()
    for href in read_spine(zf):
        parser = BlockParser()
        try:
            parser.feed(zf.read(href).decode("utf-8"))
        except UnicodeDecodeError:
            parser.feed(zf.read(href).decode("gb18030"))
        for tag, text in parser.blocks:
            if tag in ("h1", "h2"):
                if text in seen_titles:  # 每章头部重复书名
                    continue
                seen_titles.add(text)
                out += [f"# {text}", ""]
            elif tag in ("h3", "h4"):
                out += [f"## {text}", ""]
            else:
                out += [text, ""]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    chapters = sum(1 for l in out if l.startswith("# "))
    print(f"wrote {out_path} ({chapters} chapters, {len(out)} lines)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    convert(sys.argv[1], sys.argv[2])
