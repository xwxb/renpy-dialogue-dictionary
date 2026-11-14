# =============================================================================
# RPYDict — Ren'Py Embedded SQLite Dictionary (v0.2.0-alpha)
# =============================================================================
# Core Features (Confirmed Stable):
# 1. Universal Column Parsing & Full-Row Fallback:
#    - Uses explicit `builtins.list` / `builtins.dict` to avoid Ren'Py's
#      `RevertableList` / `RevertableDict` isinstance() trap on `json.loads()`.
#    - Unified adapter (`_UniversalAdapter`): supports OpenLeaf WITHOUT ROWID B-tree
#      binary search, indexed normal table B-tree binary search, and in-memory scan.
#    - Preset columns (ipa, pos, definition/meaning/translation, example, synonyms,
#      points_to/lemma) get styled formatting; any unrecognized or unparsed columns
#      always fall through to `raw` / `all_columns` so the entire row is displayed.
# 2. Automatic Inflection / Plural Downgrade (`points_to`):
#    - Fixed a bug where empty JSON `"[]"` or `points_to`/`ipa` in `all_columns`
#      made pure redirect rows (like plurals/inflections with no own definitions)
#      appear non-empty and require a manual `see:` click. Now 100% auto-downgrades
#      to the target lemma whenever an entry has no definitions of its own.
#    - Enlarged the `see: <lemma>` button (`padding (10, 5)`, `text_size 16`, pill
#      background) when an entry has both its own senses and a related lemma.
# 3. Direct Dialogue Click & Translucent Bubble Popup:
#    - Hooks `config.say_menu_text_filter` to wrap dialogue words in `{a=rpydict:w}w{/a}`.
#    - Captures tap coordinates via `renpy.get_mouse_pos()` to pop up a narrow,
#      scrollbar-free, translucent bubble near the tapped word (or docked right).
#
# Experimental / Under-Observation Features (Marked with [EXPERIMENTAL] in code):
# - [EXPERIMENTAL #1 - Auto vs Fixed Bubble Height]: `persistent.rpydict_auto_height`
#   toggles between content-hugging height (`Auto Fit`) and constant height (`Fixed`).
# - [EXPERIMENTAL #2 - In-Bubble Recursive Lookup & Mini History Stack]: Tapping any
#   word inside a definition jumps to that word and pushes the previous word onto
#   `rt().history_stack`, showing a `<- prev_word` back button in the bubble header.
# - [EXPERIMENTAL #3 - Font Size & Opacity Step Controls]: `[-]` / `[+]` step buttons
#   in Setup for tweaking font size and bubble opacity without typing numbers.
#
# Future Roadmap (Comments Only — Not Implemented Yet):
# - TODO (Roadmap #1 - Gesture Summon): Hide corner "D" button and summon via gesture.
# - TODO (Roadmap #2 - Free Dragging for Trigger): Wrap corner "D" in a `drag`.
# =============================================================================

default persistent.rpydict_dir = None
default persistent.rpydict_source = None
default persistent.rpydict_btn_corner = 0
default persistent.rpydict_inline_mode = True
default persistent.rpydict_recursive_mode = True
default persistent.rpydict_right_dock = False
default persistent.rpydict_auto_height = True
default persistent.rpydict_font_step = 1
default persistent.rpydict_alpha_step = 2

init -10 python in _rpydict:

    import builtins
    import struct
    import collections

    _LIST_TYPES = (builtins.list, builtins.tuple, list, tuple)
    _DICT_TYPES = (builtins.dict, dict)
    _STR_TYPES = (builtins.str,)
    _BYTES_TYPES = (builtins.bytes, builtins.bytearray, memoryview)

    SQLITE_MAGIC = b"SQLite format 3\x00"

    _SR_INT_SIZES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8}

    _NOCASE_FOLD = {c: c + 32 for c in range(65, 91)}

    _COL_KEYWORDS = {"FOREIGN", "PRIMARY", "CONSTRAINT", "UNIQUE", "CHECK"}


    class SQLiteReadError(Exception):
        pass


    def _read_varint(buf, pos):
        """SQLite 变长整数：大端 1~9 字节，前 8 字节各贡献 7 位，第 9 字节满 8 位。"""
        result = 0
        for _i in range(8):
            b = buf[pos]
            pos += 1
            result = (result << 7) | (b & 0x7F)
            if not (b & 0x80):
                return result, pos
        b = buf[pos]
        pos += 1
        result = (result << 8) | b
        return result, pos


    def _decode_record(payload):
        """record 载荷 → 字段值列表。serial type：0/8/9 特殊，1~6 有符号大端整数，
        7 float64，>=12 偶数 BLOB、奇数 TEXT（UTF-8）。"""
        hdr_len, pos = _read_varint(payload, 0)
        serials = []
        while pos < hdr_len:
            st, pos = _read_varint(payload, pos)
            serials.append(st)
        values = []
        body = hdr_len
        for st in serials:
            if st == 0:
                values.append(None)
            elif st == 8:
                values.append(0)
            elif st == 9:
                values.append(1)
            elif 1 <= st <= 6:
                n = _SR_INT_SIZES[st]
                values.append(int.from_bytes(payload[body:body + n], "big", signed=True))
                body += n
            elif st == 7:
                values.append(struct.unpack(">d", payload[body:body + 8])[0])
                body += 8
            elif st >= 12:
                n = (st - 12) // 2
                raw = payload[body:body + n]
                body += n
                values.append(raw.decode("utf-8", "replace") if st % 2 else builtins.bytes(raw))
            else:
                raise SQLiteReadError("unsupported serial type %d" % st)
        return values


    def _nocase_str(v):
        """SQLite NOCASE collation 语义的键归一（仅折 ASCII 大写）。"""
        if isinstance(v, _BYTES_TYPES):
            try:
                v = builtins.bytes(v).decode("utf-8", "replace")
            except Exception:
                v = str(v)
        return str(v).translate(_NOCASE_FOLD)


    def _split_top_level(s):
        """按顶层逗号切分（括号内/引号内不切），供 CREATE TABLE / INDEX 列解析。"""
        parts = []
        cur = []
        depth = 0
        quote = None
        for ch in s:
            if quote:
                cur.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "'\"`":
                quote = ch
                cur.append(ch)
            elif ch == "(":
                depth += 1
                cur.append(ch)
            elif ch == ")":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur))
        return parts


    def _parse_create_table(sql):
        """解析 CREATE TABLE：列名列表、PK 列名、WITHOUT ROWID 判定、rowid 别名列及自动索引列。"""
        if not sql:
            raise SQLiteReadError("table without sql")
        clean_lines = []
        for ln in sql.splitlines():
            idx = ln.find("--")
            clean_lines.append(ln[:idx] if idx >= 0 else ln)
        sql = "\n".join(clean_lines)
        p = sql.find("(")
        if p < 0:
            raise SQLiteReadError("cannot parse table sql")
        depth = 0
        q = -1
        for i in range(p, len(sql)):
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
                if depth == 0:
                    q = i
                    break
        if q < 0:
            raise SQLiteReadError("unbalanced create table sql")
        cols = []
        pk = []
        autoindexes = []
        rowid_alias_col = None
        without = "WITHOUT ROWID" in sql[q:].upper()
        for part in _split_top_level(sql[p + 1:q]):
            toks = part.strip().split()
            if not toks:
                continue
            name = toks[0].strip('"`[]')
            up_part = " ".join(t.upper() for t in toks)
            if name.upper() in _COL_KEYWORDS:
                if ("PRIMARY KEY" in up_part or "UNIQUE" in up_part) and "(" in part:
                    inner = part[part.find("(") + 1:part.rfind(")")]
                    c_list = []
                    for c in inner.split(","):
                        cn = c.strip().strip('"`[]').split()[0] if c.strip() else ""
                        if cn:
                            c_list.append(cn)
                    if "PRIMARY KEY" in up_part:
                        for cn in c_list:
                            if cn not in pk:
                                pk.append(cn)
                    if c_list and not without:
                        autoindexes.append(c_list)
                continue
            cols.append(name)
            is_int_pk = ("INTEGER PRIMARY KEY" in up_part and "DESC" not in up_part and not without)
            if "PRIMARY KEY" in up_part and name not in pk:
                pk.append(name)
                if is_int_pk:
                    rowid_alias_col = name
                elif not without:
                    autoindexes.append([name])
            elif "UNIQUE" in up_part and not without:
                autoindexes.append([name])
        if without:
            rowid_alias_col = None
            if not pk:
                pk = cols[:1]
        elif len(pk) > 1:
            rowid_alias_col = None
        return cols, pk, without, rowid_alias_col, autoindexes


    def _parse_create_index(sql):
        if not sql:
            return []
        p = sql.find("(")
        q = sql.rfind(")")
        if p < 0 or q <= p:
            return []
        out = []
        for c in _split_top_level(sql[p + 1:q]):
            toks = c.strip().split()
            if toks:
                out.append(toks[0].strip('"`[]'))
        return out


    class ReadOnlySQLite(object):
        """SQLite 文件只读读取器。"""

        def __init__(self, path):
            self.path = path
            self._cache = collections.OrderedDict()
            db = None
            try:
                db = open(path, "rb")
                db.seek(0, 2)
                if db.tell() < 100:
                    raise SQLiteReadError("file too small: %s" % path)
                db.seek(0)
                header = db.read(100)
                if header[:16] != SQLITE_MAGIC:
                    raise SQLiteReadError("not a sqlite database: %s" % path)
                ps = struct.unpack(">H", header[16:18])[0]
                self.page_size = 65536 if ps == 1 else ps
                self.reserved = header[20]
                self.usable = self.page_size - self.reserved
                enc = struct.unpack(">I", header[56:60])[0]
                if enc not in (0, 1):
                    raise SQLiteReadError("only utf-8 databases supported")
                if self.usable < 480:
                    raise SQLiteReadError("unreasonable page size")
            except Exception:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass
                raise
            self.f = db
            self._schema = None
            self._tables = {}

        def close(self):
            try:
                self.f.close()
            except Exception:
                pass

        def _read_file(self, offset, n):
            self.f.seek(offset)
            data = self.f.read(n)
            if len(data) != n:
                raise SQLiteReadError("unexpected EOF at %d" % offset)
            return data

        def page(self, pgno):
            c = self._cache
            hit = c.get(pgno)
            if hit is not None:
                c.move_to_end(pgno)
                return hit
            data = self._read_file((pgno - 1) * self.page_size, self.page_size)
            if len(c) >= 128:
                c.popitem(last=False)
            c[pgno] = data
            return data

        def _local_size(self, payload_len, index_page):
            u = self.usable
            maxlocal = (u - 12) * 64 // 255 - 23 if index_page else u - 35
            if payload_len <= maxlocal:
                return payload_len, 0
            minlocal = (u - 12) * 32 // 255 - 23
            k = minlocal + (payload_len - minlocal) % (u - 4)
            local = k if k <= maxlocal else minlocal
            return local, payload_len - local

        def _read_payload(self, page, pos, payload_len, index_page):
            local, remain = self._local_size(payload_len, index_page)
            data = bytearray(page[pos:pos + local])
            pos += local
            if remain:
                next_pg = struct.unpack(">I", page[pos:pos + 4])[0]
                pos += 4
                u = self.usable
                while remain > 0 and next_pg:
                    op = self.page(next_pg)
                    take = min(remain, u - 4)
                    data.extend(op[4:4 + take])
                    remain -= take
                    next_pg = struct.unpack(">I", op[:4])[0]
                if remain > 0:
                    raise SQLiteReadError("broken overflow chain")
            return builtins.bytes(data), pos

        def _btree_header(self, pgno):
            page = self.page(pgno)
            base = 100 if pgno == 1 else 0
            ptype = page[base]
            ncells = struct.unpack(">H", page[base + 3:base + 5])[0]
            rightmost = None
            hdrlen = 8
            if ptype in (2, 5):
                rightmost = struct.unpack(">I", page[base + 8:base + 12])[0]
                hdrlen = 12
            cell_ptrs = []
            for i in range(ncells):
                cell_ptrs.append(struct.unpack(">H", page[base + hdrlen + 2 * i:base + hdrlen + 2 * i + 2])[0])
            return ptype, cell_ptrs, rightmost

        def schema(self):
            if self._schema is None:
                out = []
                for r in self._walk_table(1):
                    out.append({
                        "type": r[0],
                        "name": r[1],
                        "tbl_name": r[2],
                        "rootpage": r[3],
                        "sql": r[4],
                    })
                self._schema = out
            return self._schema

        def table_meta(self, tbl_name):
            m = self._tables.get(tbl_name)
            if m is not None:
                return m
            ent = None
            for s in self.schema():
                if s["type"] == "table" and s["name"] == tbl_name:
                    ent = s
                    break
            if ent is None:
                raise SQLiteReadError("table not found: %s" % tbl_name)
            cols, pk, without, rowid_alias_col, autoindexes = _parse_create_table(ent["sql"])
            record_cols = cols
            if without:
                record_cols = pk + [c for c in cols if c not in pk]
            nocase = "NOCASE" in (ent["sql"] or "").upper()
            rowid_alias_idx = (
                record_cols.index(rowid_alias_col)
                if (rowid_alias_col and rowid_alias_col in record_cols)
                else None
            )
            indexes = []
            auto_idx = 0
            for s in self.schema():
                if s["type"] == "index" and s["tbl_name"] == tbl_name and s["rootpage"]:
                    if s["sql"]:
                        idx_cols = _parse_create_index(s["sql"])
                        idx_nocase = "NOCASE" in s["sql"].upper()
                    else:
                        idx_cols = (
                            autoindexes[auto_idx]
                            if auto_idx < len(autoindexes)
                            else (pk if pk else [])
                        )
                        idx_nocase = nocase
                        auto_idx += 1
                    indexes.append({
                        "name": s["name"],
                        "rootpage": s["rootpage"],
                        "columns": idx_cols,
                        "nocase": idx_nocase,
                    })
            m = {
                "columns": cols,
                "record_columns": record_cols,
                "pk": pk,
                "without_rowid": without,
                "nocase": nocase,
                "rowid_alias_idx": rowid_alias_idx,
                "rootpage": ent["rootpage"],
                "indexes": indexes,
            }
            self._tables[tbl_name] = m
            return m

        def _walk_table(self, pgno, rowid_alias_idx=None):
            ptype, cell_ptrs, rightmost = self._btree_header(pgno)
            if ptype == 13:
                page = self.page(pgno)
                for cp in cell_ptrs:
                    plen, pos = _read_varint(page, cp)
                    rowid, pos = _read_varint(page, pos)
                    payload, _p = self._read_payload(page, pos, plen, False)
                    rec = _decode_record(payload)
                    if rowid_alias_idx is not None and rowid_alias_idx < len(rec) and rec[rowid_alias_idx] is None:
                        rec[rowid_alias_idx] = rowid
                    yield rec
            elif ptype == 5:
                page = self.page(pgno)
                for cp in cell_ptrs:
                    left = struct.unpack(">I", page[cp:cp + 4])[0]
                    for row in self._walk_table(left, rowid_alias_idx):
                        yield row
                for row in self._walk_table(rightmost, rowid_alias_idx):
                    yield row
            else:
                raise SQLiteReadError("page %d: not a table b-tree page (%d)" % (pgno, ptype))

        def _walk_index(self, pgno, depth):
            if depth > 25:
                raise SQLiteReadError("b-tree too deep")
            ptype, cell_ptrs, rightmost = self._btree_header(pgno)
            page = self.page(pgno)
            if ptype == 10:
                for cp in cell_ptrs:
                    plen, pos = _read_varint(page, cp)
                    payload, _p = self._read_payload(page, pos, plen, True)
                    yield _decode_record(payload)
            elif ptype == 2:
                for cp in cell_ptrs:
                    left = struct.unpack(">I", page[cp:cp + 4])[0]
                    plen, pos = _read_varint(page, cp + 4)
                    payload, _p = self._read_payload(page, pos, plen, True)
                    for row in self._walk_index(left, depth + 1):
                        yield row
                    yield _decode_record(payload)
                for row in self._walk_index(rightmost, depth + 1):
                    yield row
            else:
                raise SQLiteReadError("page %d: not an index b-tree page (%d)" % (pgno, ptype))

        def iter_rows(self, tbl_name):
            m = self.table_meta(tbl_name)
            if m["without_rowid"]:
                for row in self._walk_index(m["rootpage"], 0):
                    yield row
            else:
                for row in self._walk_table(m["rootpage"], m["rowid_alias_idx"]):
                    yield row

        def rowid_search(self, tbl_name, target_rowid):
            m = self.table_meta(tbl_name)
            if m["without_rowid"]:
                return None
            return self._search_table_rowid(m["rootpage"], target_rowid, m["rowid_alias_idx"], 0)

        def _search_table_rowid(self, pgno, target_rowid, rowid_alias_idx, depth):
            if depth > 25:
                return None
            ptype, cell_ptrs, rightmost = self._btree_header(pgno)
            page = self.page(pgno)
            if ptype == 13:
                for cp in cell_ptrs:
                    plen, pos = _read_varint(page, cp)
                    rowid, pos = _read_varint(page, pos)
                    if rowid == target_rowid:
                        payload, _p = self._read_payload(page, pos, plen, False)
                        rec = _decode_record(payload)
                        if rowid_alias_idx is not None and rowid_alias_idx < len(rec) and rec[rowid_alias_idx] is None:
                            rec[rowid_alias_idx] = rowid
                        return rec
                    if rowid > target_rowid:
                        return None
                return None
            elif ptype == 5:
                for cp in cell_ptrs:
                    left = struct.unpack(">I", page[cp:cp + 4])[0]
                    ikey, _ = _read_varint(page, cp + 4)
                    if target_rowid <= ikey:
                        return self._search_table_rowid(left, target_rowid, rowid_alias_idx, depth + 1)
                return self._search_table_rowid(rightmost, target_rowid, rowid_alias_idx, depth + 1)
            return None

        def index_search(self, tbl_name, key):
            m = self.table_meta(tbl_name)
            if not m["without_rowid"]:
                raise SQLiteReadError("index_search requires WITHOUT ROWID table: %s" % tbl_name)
            nc = m.get("nocase", True)
            res = self._search_index(m["rootpage"], _nocase_str(key) if nc else str(key), 0, nc)
            if res is None:
                alt = not nc
                res = self._search_index(m["rootpage"], _nocase_str(key) if alt else str(key), 0, alt)
            return res

        def index_rowid_search(self, idx_rootpage, key, nocase=False):
            res = self._search_index(idx_rootpage, _nocase_str(key) if nocase else str(key), 0, nocase)
            if res is None:
                alt = not nocase
                res = self._search_index(idx_rootpage, _nocase_str(key) if alt else str(key), 0, alt)
            if res and len(res) >= 2 and isinstance(res[-1], int):
                return res[-1]
            return None

        def _search_index(self, pgno, key, depth, nocase=True):
            if depth > 25:
                raise SQLiteReadError("b-tree too deep")
            ptype, cell_ptrs, rightmost = self._btree_header(pgno)
            page = self.page(pgno)
            if ptype == 10:
                for cp in cell_ptrs:
                    plen, pos = _read_varint(page, cp)
                    payload, _p = self._read_payload(page, pos, plen, True)
                    vals = _decode_record(payload)
                    if not vals or vals[0] is None:
                        continue
                    k = _nocase_str(vals[0]) if nocase else str(vals[0])
                    if k == key:
                        return vals
                    if k > key:
                        return None
                return None
            elif ptype == 2:
                for cp in cell_ptrs:
                    left = struct.unpack(">I", page[cp:cp + 4])[0]
                    plen, pos = _read_varint(page, cp + 4)
                    payload, _p = self._read_payload(page, pos, plen, True)
                    vals = _decode_record(payload)
                    if not vals or vals[0] is None:
                        continue
                    k = _nocase_str(vals[0]) if nocase else str(vals[0])
                    if k == key:
                        return vals
                    if k > key:
                        return self._search_index(left, key, depth + 1, nocase)
                return self._search_index(rightmost, key, depth + 1, nocase)
            raise SQLiteReadError("page %d: not an index b-tree page (%d)" % (pgno, ptype))


init python in _rpydict:

    import builtins
    import sys
    import os
    import re
    import json
    import zlib
    import types

    MOD_VERSION = "0.2.0-alpha"

    GENERIC_SCAN_LIMIT = 400000

    # [EXPERIMENTAL #3 - 字号与透明度预设档位调节]
    # 默认档位为 index=1 ("Default", offset=0)，保持你习惯的基准字号，同时支持在 Setup 中用 - / + 按钮微调。
    FONT_STEPS = [
        ("S", -2),
        ("Default", 0),
        ("M+", 2),
        ("L", 4),
        ("XL", 7),
    ]

    ALPHA_STEPS = [
        ("30%", "#0c10184d", "#12141c66"),
        ("50%", "#0c101880", "#12141c99"),
        ("65%", "#0c1018a8", "#12141cc0"),
        ("80%", "#0c1018cc", "#12141ce0"),
        ("92%", "#0c1018eb", "#12141cf4"),
    ]

    POS_LABELS = {
        1: "noun", 2: "verb", 3: "adj", 4: "adv", 5: "pron",
        6: "prep", 7: "conj", 8: "interj", 9: "phrase", 10: "art",
        11: "num", 12: "abbr", 13: "prefix", 14: "suffix",
    }

    GENERIC_WORD_COLS = (
        "word", "term", "headword", "entry", "expression",
        "vocab", "kanji", "title", "name", "key",
    )

    GENERIC_ALIASES = {
        "pos": "pos", "part_of_speech": "pos", "word_type": "pos",
        "lexical_category": "pos",
        "definition": "definition", "definitions": "definition",
        "def": "definition", "defs": "definition",
        "gloss": "definition", "glosses": "definition",
        "meaning": "definition", "meanings": "definition",
        "sense": "definition", "senses": "definition",
        "translation": "definition", "translations": "definition",
        "trans": "definition", "explain": "definition",
        "explanation": "definition", "zh": "definition", "cn": "definition",
        "content": "definition", "detail": "definition", "details": "definition",
        "description": "definition", "desc": "definition", "text": "definition",
        "body": "definition",
        "example": "example", "examples": "example",
        "sentence": "example", "sentences": "example",
        "sample": "example", "usage": "example",
        "synonyms": "synonyms", "syn": "synonyms",
        "synonym": "synonyms", "syns": "synonyms",
        "ipa": "ipa", "pron": "ipa", "pronunciation": "ipa",
        "phonetic": "ipa", "phonetics": "ipa", "uk_phonetic": "ipa",
        "us_phonetic": "ipa", "reading": "ipa", "kana": "ipa", "pinyin": "ipa",
        "points_to": "points_to", "lemma": "points_to",
        "root": "points_to", "base": "points_to", "stem": "points_to",
        "redirect": "points_to",
    }

    IRREGULAR = {
        "did": "do", "does": "do", "done": "do", "doing": "do",
        "went": "go", "gone": "go", "goes": "go",
        "was": "be", "were": "be", "been": "be", "is": "be", "are": "be", "am": "be",
        "had": "have", "has": "have", "having": "have",
        "made": "make", "makes": "make",
        "ran": "run", "runs": "run",
        "said": "say", "says": "say",
        "saw": "see", "seen": "see", "sees": "see",
        "took": "take", "taken": "take", "takes": "take",
        "got": "get", "gotten": "get", "gets": "get",
        "came": "come", "comes": "come",
        "knew": "know", "known": "know", "knows": "know",
        "told": "tell", "tells": "tell",
        "felt": "feel", "feels": "feel",
        "left": "leave", "leaves": "leave",
        "kept": "keep", "keeps": "keep",
        "slept": "sleep", "met": "meet", "meets": "meet",
        "paid": "pay", "built": "build", "sent": "send", "spent": "spend",
        "found": "find", "thought": "think", "brought": "bring",
        "bought": "buy", "caught": "catch", "taught": "teach",
        "lost": "lose", "won": "win", "held": "hold", "sat": "sit",
        "stood": "stand", "understood": "understand",
        "ate": "eat", "eaten": "eat", "wrote": "write", "written": "write",
        "drove": "drive", "driven": "drive", "gave": "give", "given": "give",
        "began": "begin", "begun": "begin", "fell": "fall", "fallen": "fall",
        "spoke": "speak", "spoken": "speak", "broke": "break", "broken": "break",
        "chose": "choose", "chosen": "choose", "wore": "wear", "worn": "wear",
        "threw": "throw", "thrown": "throw", "grew": "grow", "grown": "grow",
        "drew": "draw", "drawn": "draw", "flew": "fly", "flown": "fly",
        "children": "child", "men": "man", "women": "woman", "feet": "foot",
        "teeth": "tooth", "mice": "mouse", "geese": "goose", "lives": "life",
    }

    _CONS = "bcdfghjklmnpqrstvwxz"

    if "_rpydict_rt" not in sys.modules:
        _m = types.ModuleType("_rpydict_rt")
        _m.db = None
        _m.adapter = None
        _m.source_name = None
        _m.sources = []
        _m.scanned = False
        _m.tokens = []
        _m.last_raw_what = ""
        _m.last_error = None
        _m.scan_errors = []
        _m.lookup_cache = {}
        _m.popup_pos = (0.97, 0.42, 1.0, 0.5)
        _m.current_word = None
        _m.history_stack = []
        sys.modules["_rpydict_rt"] = _m
        del _m

    for _attr, _def_val in (
        ("scan_errors", []),
        ("last_raw_what", ""),
        ("popup_pos", (0.97, 0.42, 1.0, 0.5)),
        ("current_word", None),
        ("history_stack", []),
    ):
        if not hasattr(sys.modules["_rpydict_rt"], _attr):
            setattr(sys.modules["_rpydict_rt"], _attr, _def_val)

    def rt():
        return sys.modules["_rpydict_rt"]

    def esc_text(s):
        """用于带 substitute False 的 text 控件：只需转义左花括号与隅付き括弧。"""
        if s is None:
            return ""
        return str(s).replace("{", "{{").replace("\u3010", "\u3010\u3010")

    def esc_btn(s):
        """用于 textbutton 标题：同时转义 [ 与 {，无需传 text_substitute。"""
        if s is None:
            return ""
        return str(s).replace("[", "[[").replace("{", "{{").replace("\u3010", "\u3010\u3010")

    def _android_hook(name, *args):
        fn = globals().get(name)
        if callable(fn):
            return fn(*args)
        return None


    def candidate_base_dirs():
        import store
        dirs = []
        pd = store.persistent.rpydict_dir
        if pd:
            dirs.append(pd)
        if renpy.android:
            dirs.append("/storage/emulated/0/Documents/rpydict")
            dirs.append("/sdcard/Documents/rpydict")
            x = _android_hook("android_ext_files_dir")
            if x:
                dirs.append(x)
        else:
            g = renpy.config.gamedir
            dirs.append(os.path.join(g, "rpydict"))
            dirs.append(os.path.normpath(os.path.join(g, "..", "rpydict")))
            home = os.path.expanduser("~")
            dirs.append(os.path.join(home, "Documents", "rpydict"))
            dirs.append(os.path.join(home, "Downloads", "rpydict"))
        seen = set()
        out = []
        for d in dirs:
            if d and d not in seen:
                seen.add(d)
                out.append(d)
        return out

    def _find_db_in(dirpath, errors=None):
        found = []
        try:
            entries = sorted(os.listdir(dirpath))
        except Exception as e:
            if errors is not None:
                errors.append("Cannot read %s: %s" % (dirpath, e))
            return found
        for name in entries:
            p = os.path.join(dirpath, name)
            if os.path.isdir(p):
                try:
                    files = sorted(os.listdir(p))
                except Exception as e:
                    if errors is not None:
                        errors.append("Cannot read %s: %s" % (p, e))
                    continue
                for f in files:
                    if f.lower().endswith((".db", ".sqlite", ".sqlite3")):
                        found.append((name, os.path.join(p, f)))
                        break
            elif name.lower().endswith((".db", ".sqlite", ".sqlite3")):
                found.append((os.path.splitext(name)[0], p))
        return found

    def rescan_sources():
        sources = []
        errors = []
        for base in candidate_base_dirs():
            if os.path.isdir(base):
                sources.extend(_find_db_in(base, errors))
            else:
                errors.append("Folder not found or inaccessible: %s" % base)
        seen = set()
        out = []
        for n, p in sources:
            if n not in seen:
                seen.add(n)
                out.append((n, p))
        _rt = rt()
        _rt.sources = out
        _rt.scan_errors = errors
        _rt.scanned = True
        for error in errors:
            try:
                renpy.log("[RPYDict] scan: %s" % error)
            except Exception:
                pass
        return out


    def _coerce_synonyms(v):
        if not v:
            return []
        if isinstance(v, _LIST_TYPES):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        if isinstance(v, _BYTES_TYPES):
            try:
                v = builtins.bytes(v).decode("utf-8", "replace")
            except Exception:
                return []
        if isinstance(v, _STR_TYPES):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                try:
                    j = json.loads(s)
                    if isinstance(j, _LIST_TYPES):
                        return [str(x).strip() for x in j if x is not None and str(x).strip()]
                except Exception:
                    pass
            return [p.strip() for p in re.split(r"[,;/|]", s) if p.strip()]
        return [str(v)]

    def _try_decompress_bytes(raw_bytes, preset=None):
        b = builtins.bytes(raw_bytes)
        if not b:
            return None
        attempts = []
        if preset:
            attempts.append((-15, preset))
            attempts.append((15, preset))
        attempts.extend([(-15, None), (15, None), (31, None), (47, None)])
        for wbits, zd in attempts:
            try:
                if zd is not None:
                    d = zlib.decompressobj(wbits, zdict=zd)
                else:
                    d = zlib.decompressobj(wbits)
                out = d.decompress(b) + d.flush()
                if out:
                    return out.decode("utf-8", "replace")
            except Exception:
                continue
        try:
            return b.decode("utf-8")
        except Exception:
            return b.decode("utf-8", "replace")

    def _format_json_readable(obj):
        if obj is None:
            return ""
        if isinstance(obj, _STR_TYPES):
            return obj.strip()
        if isinstance(obj, (int, float, bool)):
            return str(obj)
        if isinstance(obj, _LIST_TYPES):
            parts = [_format_json_readable(x) for x in obj]
            parts = [p for p in parts if p]
            return " ; ".join(parts)
        if isinstance(obj, _DICT_TYPES):
            parts = []
            for k, v in obj.items():
                fv = _format_json_readable(v)
                if fv:
                    parts.append("%s: %s" % (k, fv))
            return " | ".join(parts)
        return str(obj)

    def _decode_cell(val, preset=None):
        """解码任意单元格，返回 (可读字符串, 解析后的JSON对象或None)。
        注意：若解压后为空数组 '[]'、空对象 '{}' 或空值，严格返回 (None, None)，
        防止无词义的复数/变形词把空 '[]' 混入 raw 从而阻塞 points_to 自动降级！"""
        if val is None:
            return None, None
        txt = None
        if isinstance(val, _BYTES_TYPES):
            txt = _try_decompress_bytes(val, preset)
        elif isinstance(val, _STR_TYPES):
            txt = val
        else:
            txt = str(val)
        if txt is None:
            return None, None
        txt = txt.strip()
        if not txt or txt in ("[]", "{}", "null", "None"):
            return None, None
        parsed = None
        if (txt.startswith("[") and txt.endswith("]")) or (txt.startswith("{") and txt.endswith("}")):
            try:
                parsed = json.loads(txt)
            except Exception:
                parsed = None
        if parsed is not None:
            if isinstance(parsed, (_LIST_TYPES, _DICT_TYPES)) and len(parsed) == 0:
                return None, None
            readable = _format_json_readable(parsed)
            if not readable:
                return None, None
        else:
            readable = txt
        return readable, parsed

    def _normalize_pos(p):
        if p is None:
            return None
        if isinstance(p, int):
            return POS_LABELS.get(p, str(p))
        s = str(p).strip()
        if not s:
            return None
        if s.isdigit():
            return POS_LABELS.get(int(s), s)
        return s

    def _extract_senses_from_parsed(parsed, txt, default_pos=None, default_ex=None, default_syn=None):
        senses = []
        if parsed is not None:
            if isinstance(parsed, _LIST_TYPES):
                for m in parsed:
                    if isinstance(m, _LIST_TYPES):
                        if len(m) == 0:
                            continue
                        if len(m) == 1:
                            df = _format_json_readable(m[0])
                            if df:
                                senses.append({
                                    "pos": default_pos,
                                    "definition": df,
                                    "example": default_ex,
                                    "synonyms": default_syn or [],
                                })
                        else:
                            pos_val = _normalize_pos(m[0]) or default_pos
                            df = _format_json_readable(m[1])
                            ex = _format_json_readable(m[2]) if len(m) > 2 and m[2] else default_ex
                            syn = _coerce_synonyms(m[3]) if len(m) > 3 and m[3] else (default_syn or [])
                            if df:
                                senses.append({
                                    "pos": pos_val,
                                    "definition": df,
                                    "example": ex or None,
                                    "synonyms": syn,
                                })
                    elif isinstance(m, _DICT_TYPES):
                        pos_val = _normalize_pos(
                            m.get("pos") or m.get("part_of_speech") or m.get("type") or m.get("p")
                        ) or default_pos
                        df_raw = (
                            m.get("definition") or m.get("def") or m.get("meaning")
                            or m.get("gloss") or m.get("translation") or m.get("trans")
                            or m.get("d") or m.get("text") or m.get("explain")
                        )
                        df = _format_json_readable(df_raw) if df_raw is not None else _format_json_readable(m)
                        ex_raw = m.get("example") or m.get("examples") or m.get("sentence") or m.get("e") or m.get("s")
                        ex = _format_json_readable(ex_raw) if ex_raw is not None else default_ex
                        syn_raw = m.get("synonyms") or m.get("syn") or m.get("syns")
                        syn = _coerce_synonyms(syn_raw) if syn_raw is not None else (default_syn or [])
                        if df:
                            senses.append({
                                "pos": pos_val,
                                "definition": df,
                                "example": ex or None,
                                "synonyms": syn,
                            })
                    elif m is not None:
                        df = _format_json_readable(m)
                        if df:
                            senses.append({
                                "pos": default_pos,
                                "definition": df,
                                "example": default_ex,
                                "synonyms": default_syn or [],
                            })
            elif isinstance(parsed, _DICT_TYPES):
                has_def_key = any(
                    k in parsed
                    for k in ("definition", "def", "meaning", "gloss", "translation", "trans", "text", "explain")
                )
                if has_def_key:
                    pos_val = _normalize_pos(
                        parsed.get("pos") or parsed.get("part_of_speech") or parsed.get("type")
                    ) or default_pos
                    df_raw = (
                        parsed.get("definition") or parsed.get("def") or parsed.get("meaning")
                        or parsed.get("gloss") or parsed.get("translation") or parsed.get("trans")
                        or parsed.get("text") or parsed.get("explain")
                    )
                    df = _format_json_readable(df_raw)
                    ex_raw = parsed.get("example") or parsed.get("examples") or parsed.get("sentence")
                    ex = _format_json_readable(ex_raw) if ex_raw is not None else default_ex
                    syn_raw = parsed.get("synonyms") or parsed.get("syn")
                    syn = _coerce_synonyms(syn_raw) if syn_raw is not None else (default_syn or [])
                    if df:
                        senses.append({
                            "pos": pos_val,
                            "definition": df,
                            "example": ex or None,
                            "synonyms": syn,
                        })
                else:
                    for k, v in parsed.items():
                        if isinstance(v, _LIST_TYPES):
                            for item in v:
                                df = _format_json_readable(item)
                                if df:
                                    senses.append({
                                        "pos": _normalize_pos(k) or default_pos,
                                        "definition": df,
                                        "example": None,
                                        "synonyms": [],
                                    })
                        else:
                            df = _format_json_readable(v)
                            if df:
                                senses.append({
                                    "pos": _normalize_pos(k) or default_pos,
                                    "definition": df,
                                    "example": None,
                                    "synonyms": [],
                                })
        elif txt:
            senses.append({
                "pos": default_pos,
                "definition": txt,
                "example": default_ex,
                "synonyms": default_syn or [],
            })
        return senses


    class _UniversalAdapter(object):
        """统一 SQLite 词典适配器：兼容 OpenLeaf 与任意自定义表结构，带 100% 整行兜底展示。"""

        def __init__(self, db):
            self.db = db
            self.preset = None
            self.table_name = None
            self.table_meta = None
            self.columns = []
            self.word_col = None
            self.rowid_alias_col = None
            self.without_rowid = False
            self.word_index = None
            self.index = {}
            self.use_memory_index = False
            self._init_schema()

        def _init_schema(self):
            schema_list = self.db.schema()
            for s in schema_list:
                if s["type"] == "table" and s["name"].lower() in ("meta", "metadata", "config"):
                    try:
                        for r in self.db.iter_rows(s["name"]):
                            if not r:
                                continue
                            if len(r) >= 2 and str(r[0]).lower() in ("preset_dict", "zdict", "dict", "preset") and r[1]:
                                self.preset = (
                                    builtins.bytes(r[1])
                                    if isinstance(r[1], _BYTES_TYPES)
                                    else str(r[1]).encode("latin1")
                                )
                                break
                            for cell in r:
                                if isinstance(cell, _BYTES_TYPES) and len(cell) >= 256 and self.preset is None:
                                    self.preset = builtins.bytes(cell)
                    except Exception:
                        pass

            best = None
            for s in schema_list:
                if s["type"] != "table":
                    continue
                tname = s["name"]
                if tname.lower() in ("meta", "metadata", "config") or tname.startswith("sqlite_"):
                    continue
                try:
                    m = self.db.table_meta(tname)
                except Exception:
                    continue
                cols = m["record_columns"]
                if not cols:
                    continue
                wc = None
                for c in cols:
                    if c.lower() in GENERIC_WORD_COLS:
                        wc = c
                        break
                if wc is not None:
                    best = (tname, m, wc)
                    if tname.lower() in ("words", "entries", "dictionary", "dict", "stardict"):
                        break
                elif best is None:
                    fallback_col = cols[0]
                    if m["rowid_alias_idx"] == 0 and len(cols) > 1:
                        fallback_col = cols[1]
                    best = (tname, m, fallback_col)

            if best is None:
                raise SQLiteReadError("no usable table in dictionary")

            self.table_name, self.table_meta, self.word_col = best
            self.columns = self.table_meta["record_columns"]
            r_idx = self.table_meta.get("rowid_alias_idx")
            self.rowid_alias_col = self.columns[r_idx] if (r_idx is not None and r_idx < len(self.columns)) else None
            self.without_rowid = self.table_meta["without_rowid"]

            if not self.without_rowid:
                for idx in self.table_meta.get("indexes", []):
                    idx_cols = [c.lower() for c in idx.get("columns", [])]
                    if idx_cols and idx_cols[0] == self.word_col.lower():
                        self.word_index = idx
                        break
                if self.word_index is None:
                    self.use_memory_index = True

        def init_source(self):
            if not self.use_memory_index:
                return
            n = 0
            for rec in self.db.iter_rows(self.table_name):
                n += 1
                if n > GENERIC_SCAN_LIMIT:
                    raise SQLiteReadError(
                        "dictionary too large (%d+ rows) without an index on '%s'"
                        % (GENERIC_SCAN_LIMIT, self.word_col)
                    )
                d = builtins.dict(zip(self.columns, rec))
                head = d.get(self.word_col)
                if head is None or str(head).strip() == "":
                    continue
                self.index.setdefault(str(head).strip().lower(), []).append(d)
            self.db._cache.clear()

        def _fetch_rows_for_word(self, w):
            rows = []
            if self.without_rowid:
                for cand in (w, w.lower(), w.capitalize(), w.upper()):
                    try:
                        r = self.db.index_search(self.table_name, cand)
                    except Exception:
                        r = None
                    if r is not None:
                        rows.append(builtins.dict(zip(self.columns, r)))
                        break
            elif self.word_index is not None:
                rootpg = self.word_index["rootpage"]
                nocase = self.word_index.get("nocase", False)
                for cand in (w, w.lower(), w.capitalize(), w.upper()):
                    try:
                        rowid = self.db.index_rowid_search(rootpg, cand, nocase)
                    except Exception:
                        rowid = None
                    if rowid is not None:
                        try:
                            r = self.db.rowid_search(self.table_name, rowid)
                        except Exception:
                            r = None
                        if r is not None:
                            rows.append(builtins.dict(zip(self.columns, r)))
                            break
            else:
                hit = self.index.get(w.lower())
                if hit:
                    rows.extend(hit)
            return rows

        def _build_entry(self, fallback_word, rows):
            headword = None
            ipa = None
            points_to = None
            senses = []
            raw = []
            all_columns = []
            seen_raw = set()

            for d in rows:
                decoded_row = {}
                for c in self.columns:
                    raw_val = d.get(c)
                    readable, parsed = _decode_cell(raw_val, self.preset)
                    decoded_row[c] = (readable, parsed)

                hw_read, _ = decoded_row.get(self.word_col, (None, None))
                if headword is None and hw_read:
                    headword = hw_read

                row_pos = None
                row_ex = None
                row_syn = []
                pos_cols_used = set()
                ex_cols_used = set()
                syn_cols_used = set()
                consumed_cols = {self.word_col}
                if self.rowid_alias_col:
                    consumed_cols.add(self.rowid_alias_col)

                for c in self.columns:
                    if c in consumed_cols:
                        continue
                    readable, parsed = decoded_row.get(c, (None, None))
                    if not readable:
                        continue
                    role = GENERIC_ALIASES.get(c.lower())
                    if role == "ipa" and ipa is None:
                        ipa = readable
                        consumed_cols.add(c)
                    elif role == "points_to" and points_to is None:
                        if isinstance(parsed, _LIST_TYPES) and parsed:
                            points_to = str(parsed[0]).strip()
                        else:
                            points_to = readable.split(",")[0].strip()
                        consumed_cols.add(c)
                    elif role == "pos" and row_pos is None:
                        row_pos = _normalize_pos(readable)
                        pos_cols_used.add(c)
                    elif role == "example" and row_ex is None:
                        row_ex = readable
                        ex_cols_used.add(c)
                    elif role == "synonyms" and not row_syn:
                        row_syn = _coerce_synonyms(parsed if parsed is not None else readable)
                        syn_cols_used.add(c)

                # all_columns 仅收集除词头、读音、跳转指针以外的实质内容列，避免纯跳转词条误判为有内容
                for c in self.columns:
                    if c in consumed_cols:
                        continue
                    readable, _ = decoded_row.get(c, (None, None))
                    if readable is not None and readable != "":
                        all_columns.append((c, readable))

                row_senses = []
                for c in self.columns:
                    if c in consumed_cols or c in pos_cols_used or c in ex_cols_used or c in syn_cols_used:
                        continue
                    readable, parsed = decoded_row.get(c, (None, None))
                    if not readable:
                        continue
                    role = GENERIC_ALIASES.get(c.lower())
                    if role == "definition" or (parsed is not None and isinstance(parsed, _LIST_TYPES)):
                        extracted = _extract_senses_from_parsed(parsed, readable, row_pos, row_ex, row_syn)
                        if extracted:
                            row_senses.extend(extracted)
                            consumed_cols.add(c)
                            consumed_cols.update(pos_cols_used)
                            consumed_cols.update(ex_cols_used)
                            consumed_cols.update(syn_cols_used)

                senses.extend(row_senses)

                for c in self.columns:
                    if c in consumed_cols:
                        continue
                    readable, _ = decoded_row.get(c, (None, None))
                    if readable:
                        pair = (c, readable)
                        if pair not in seen_raw:
                            seen_raw.add(pair)
                            raw.append(pair)

            if not senses and not raw and all_columns:
                for pair in all_columns:
                    if pair not in seen_raw:
                        seen_raw.add(pair)
                        raw.append(pair)

            return {
                "word": headword or fallback_word,
                "ipa": ipa,
                "points_to": points_to or None,
                "senses": senses,
                "raw": raw,
                "all_columns": all_columns,
            }

        def lookup_many(self, words):
            out = {}
            seen = set()
            for w in words:
                if not w:
                    continue
                lw = w.lower()
                if lw in seen:
                    continue
                seen.add(lw)
                rows = self._fetch_rows_for_word(w)
                if not rows:
                    continue
                e = self._build_entry(w, rows)
                out[lw] = e
                if e["word"]:
                    out[e["word"].lower()] = e
            return out


    def _close_db(db):
        try:
            db.close()
        except Exception:
            pass

    def unload_source():
        _rt = rt()
        if _rt.db is not None:
            _close_db(_rt.db)
        _rt.db = None
        _rt.adapter = None
        _rt.source_name = None
        _rt.lookup_cache.clear()

    def load_source(name):
        _rt = rt()
        if not _rt.scanned:
            rescan_sources()
        db_path = None
        for n, p in _rt.sources:
            if n == name:
                db_path = p
                break
        if db_path is None:
            _rt.last_error = "dictionary source not found: %s" % name
            return False
        db = None
        try:
            db = ReadOnlySQLite(db_path)
            adapter = _UniversalAdapter(db)
            adapter.init_source()
        except Exception as e:
            _rt.last_error = "%s: %s" % (name, e)
            if db is not None:
                _close_db(db)
            return False
        unload_source()
        _rt.db = db
        _rt.adapter = adapter
        _rt.source_name = name
        _rt.last_error = None
        return True

    def ensure_source():
        _rt = rt()
        if _rt.adapter is not None:
            return True
        import store
        if not _rt.scanned:
            rescan_sources()
        if not _rt.sources:
            _rt.last_error = "no dictionary found"
            return False
        want = store.persistent.rpydict_source
        if want and any(n == want for n, _ in _rt.sources):
            if load_source(want):
                return True
        for n, _ in _rt.sources:
            if load_source(n):
                store.persistent.rpydict_source = n
                return True
        return False

    def select_source(name):
        import store
        if load_source(name):
            store.persistent.rpydict_source = name
            renpy.restart_interaction()
            return True
        renpy.restart_interaction()
        return False

    def rescan_and_restart():
        rescan_sources()
        ensure_source()
        renpy.restart_interaction()


    def inflection_candidates(word):
        out = []
        seen = set()

        def add(x, minlen=2):
            if x and len(x) >= minlen and x not in seen:
                seen.add(x)
                out.append(x)

        w = (word or "").strip().strip("'\u2019\".,!?;:()[]{}")
        add(w, 1)
        lw = w.lower().replace("\u2019", "'")
        add(lw, 1)
        add(lw.replace("'", ""), 2)
        if lw.endswith("'s"):
            add(lw[:-2], 2)
        elif lw.endswith("s'"):
            add(lw[:-1], 2)
        for suf in ("n't", "'re", "'ve", "'ll", "'d", "'m"):
            if lw.endswith(suf) and len(lw) > len(suf) + 1:
                add(lw[:-len(suf)], 2)

        if lw in IRREGULAR:
            add(IRREGULAR[lw], 1)

        if lw.endswith("ies") and len(lw) > 4:
            add(lw[:-3] + "y")
        if lw.endswith("ied") and len(lw) > 4:
            add(lw[:-3] + "y")
        if lw.endswith("ves") and len(lw) > 4:
            add(lw[:-3] + "f")
            add(lw[:-3] + "fe")

        if lw.endswith("ing") and len(lw) > 4:
            stem = lw[:-3]
            add(stem + "e")
            add(stem)
            if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] in _CONS:
                add(stem[:-1])
        elif lw.endswith("ed") and len(lw) > 3:
            stem = lw[:-2]
            add(stem + "e")
            add(stem)
            if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] in _CONS:
                add(stem[:-1])
        elif lw.endswith("er") and len(lw) > 4:
            stem = lw[:-2]
            add(stem)
            add(stem + "e")
            if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] in _CONS:
                add(stem[:-1])
        elif lw.endswith("est") and len(lw) > 5:
            stem = lw[:-3]
            add(stem)
            add(stem + "e")
            if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] in _CONS:
                add(stem[:-1])

        if lw.endswith("es") and len(lw) > 3:
            add(lw[:-2])
        if lw.endswith("s") and not lw.endswith("ss") and len(lw) > 2:
            add(lw[:-1])
        if lw.endswith("ly") and len(lw) > 4:
            add(lw[:-2], 2)
            if lw.endswith("ily") and len(lw) > 5:
                add(lw[:-3] + "y", 2)
        return out

    def _entry_has_defs(e):
        return bool(e is not None and (e.get("senses") or e.get("raw")))

    def lookup(word):
        """查询主入口：
        - 当命中的词条自身没有任何词义（如复数/变形词仅含 points_to 跳转指针）时，
          100% 自动降级直达目标原型词条（支持最多 2 跳链式降级），无需用户再手点一次 see:；
        - 若原词在库中仅有空壳且无 points_to，还会继续尝试后续词形还原候选词；
        - 若原词自身有词义且同时带 points_to（如 running），则保留自身词义并在底部显示大号 see: 按钮。"""
        _rt = rt()
        w = (word or "").strip()
        if ":" in w:
            w = w.split(":", 1)[1].strip()
        key = w.lower()
        if not key:
            return None
        if key in _rt.lookup_cache and _rt.lookup_cache[key] is not None:
            return _rt.lookup_cache[key]
        try:
            if getattr(renpy, "predicting", lambda: False)():
                return None
        except Exception:
            pass
        if not ensure_source():
            return None
        cands = inflection_candidates(w)
        hits = _rt.adapter.lookup_many(cands)
        entry = None
        fallback_empty = None
        for c in cands:
            e = hits.get(c.lower())
            if e is None:
                continue
            if _entry_has_defs(e) or e.get("points_to"):
                entry = e
                break
            if fallback_empty is None:
                fallback_empty = e
        if entry is None:
            entry = fallback_empty

        if entry is not None:
            entry = builtins.dict(entry)
            orig_ipa = entry.get("ipa")
            entry["queried_word"] = w
            entry["lemma"] = None
            entry["redirected"] = False

            # 情况 A：当前词条自身无实质释义，但有 points_to 指向原型 → 100% 自动降级（最多追 2 跳）
            # 降级后直接以目标原型词（如 place）作为主标题与主词条，避免误导用户以为是复数专有释义
            if not _entry_has_defs(entry) and entry.get("points_to"):
                pt = entry["points_to"]
                for _hop_idx in range(2):
                    if not pt:
                        break
                    hop_cands = inflection_candidates(pt)
                    hop_hits = _rt.adapter.lookup_many(hop_cands)
                    hop = None
                    for hc in hop_cands:
                        h = hop_hits.get(hc.lower())
                        if h is not None:
                            hop = h
                            if _entry_has_defs(h):
                                break
                    if hop is None:
                        break
                    if _entry_has_defs(hop):
                        target_word = hop.get("word") or pt
                        entry = builtins.dict(hop)
                        entry["word"] = target_word
                        entry["ipa"] = hop.get("ipa") or orig_ipa
                        entry["queried_word"] = w
                        entry["redirected"] = True
                        hop_pt = hop.get("points_to")
                        if hop_pt and hop_pt.lower() not in (target_word.lower(), key):
                            entry["lemma"] = hop_pt
                        else:
                            entry["lemma"] = None
                        break
                    pt = hop.get("points_to")

            # 情况 B：如果 points_to 没救回来且自身仍无释义，尝试 cands 里的后续词形还原候选词
            if not _entry_has_defs(entry):
                for c in cands:
                    e2 = hits.get(c.lower())
                    if e2 is not None and _entry_has_defs(e2):
                        target_word = e2.get("word") or c
                        entry = builtins.dict(e2)
                        entry["word"] = target_word
                        entry["ipa"] = e2.get("ipa") or orig_ipa
                        entry["queried_word"] = w
                        entry["redirected"] = True
                        e2_pt = e2.get("points_to")
                        if e2_pt and e2_pt.lower() not in (target_word.lower(), key):
                            entry["lemma"] = e2_pt
                        else:
                            entry["lemma"] = None
                        break

            # 情况 C：自身已经有释义（如 running 或带专有复数释义的词条），保留原词标题并在底部提供 see: 按钮
            if not entry["redirected"]:
                cur_word = entry.get("word") or w
                if cur_word.lower() != key:
                    entry["redirected"] = True
                pt = entry.get("points_to")
                if pt and pt.lower() not in (cur_word.lower(), key):
                    entry["lemma"] = pt

            if len(_rt.lookup_cache) > 512:
                _rt.lookup_cache.clear()
            _rt.lookup_cache[key] = entry
        return entry

    def card_data(word):
        e = lookup(word)
        if e is not None and e.get("word"):
            rt().current_word = e["word"]
        return e

    # [EXPERIMENTAL #1 - 气泡高度自适应 vs 固定高度]
    def needs_scroll(entry):
        """当开启固定高度模式 (not persistent.rpydict_auto_height) 时，始终返回 True 使用固定高度 viewport；
        当开启自适应高度 (persistent.rpydict_auto_height) 时，中短词条直接用纯 vbox 紧贴文字高度。"""
        import store
        if not getattr(store.persistent, "rpydict_auto_height", True):
            return True
        if entry is None:
            return False
        senses = entry.get("senses") or []
        raw = entry.get("raw") or []
        if len(senses) + len(raw) > 4:
            return True
        total_chars = 0
        for s in senses:
            total_chars += len(s.get("definition") or "") + len(s.get("example") or "")
        for _k, v in raw:
            total_chars += len(v or "")
        return total_chars > 240


    # =========================================================================
    # 原文直点查词、气泡内轻量递归查词 + 优雅小栈历史记录
    # =========================================================================

    _RENPY_TAG_RE = re.compile(r"\{[^{}]*\}")
    _WORD_RE = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*")
    _SIMPLE_WORD_RE = re.compile(r"[A-Za-z]{2,}(?:['\u2019][A-Za-z]+)*")
    _INLINE_TOKEN_RE = re.compile(
        r"(\{\{|\[\[)"
        r"|(\{a=[^{}]*\}.*?\{/a\})"
        r"|(\{[^{}]*\})"
        r"|(\[[^\[\]]*\])"
        r"|([A-Za-z]{2,}(?:['\u2019][A-Za-z]+)*)",
        re.DOTALL,
    )

    def _strip_renpy_tags(s):
        s = s.replace("{{", "\x00")
        s = _RENPY_TAG_RE.sub("", s)
        return s.replace("\x00", "{")

    def inject_inline_links(text):
        """将台词中的英文单词包装为 {a=rpydict:word}word{/a}，完全跳过已有标签与 [变量]。"""
        if not text or not isinstance(text, _STR_TYPES):
            return text
        if "{a=rpydict:" in text:
            return text

        def _repl(m):
            w = m.group(5)
            if w:
                return "{a=rpydict:%s}%s{/a}" % (w, w)
            return m.group(0)

        return _INLINE_TOKEN_RE.sub(_repl, text)

    # [EXPERIMENTAL #2 - 气泡内递归查词与轻量历史栈]
    def link_card_text(s, proto="rpydict_def"):
        """气泡内部文本的递归查词包装：点击释义中的生词可继续查词，自动压入轻量历史栈。"""
        import store
        clean = esc_text(s)
        if not clean or not getattr(store.persistent, "rpydict_recursive_mode", True):
            return clean

        def _repl(m):
            w = m.group(0)
            return "{a=%s:%s}%s{/a}" % (proto, w, w)

        return _SIMPLE_WORD_RE.sub(_repl, clean)

    def open_word_fresh(word):
        """从台词或分词栏新打开一个词：清空旧历史栈，将当前词设为栈起点。"""
        w = (word or "").strip()
        if ":" in w:
            w = w.split(":", 1)[1].strip()
        if not w:
            return
        _rt = rt()
        _rt.history_stack = []
        _rt.current_word = w
        sync_card_styles()
        record_tap_pos()
        if source_ready():
            renpy.show_screen("rpydict_card", word=w)
        else:
            renpy.show_screen("rpydict_setup")
        renpy.restart_interaction()

    def jump_word_in_card(word):
        """在气泡内部点击生词/同义词/关联词跳转：将上一个词压入小栈，支持一键退回。"""
        w = (word or "").strip()
        if ":" in w:
            w = w.split(":", 1)[1].strip()
        if not w:
            return
        _rt = rt()
        cur = _rt.current_word
        if cur and cur.lower() != w.lower():
            if not _rt.history_stack or _rt.history_stack[-1].lower() != cur.lower():
                _rt.history_stack.append(cur)
                if len(_rt.history_stack) > 15:
                    _rt.history_stack.pop(0)
        _rt.current_word = w
        sync_card_styles()
        if source_ready():
            renpy.show_screen("rpydict_card", word=w)
            renpy.restart_interaction()

    def pop_word_history():
        """点击气泡顶部的 <- 按钮退回上一个查过的词。"""
        _rt = rt()
        if not _rt.history_stack:
            return
        prev_w = _rt.history_stack.pop()
        _rt.current_word = prev_w
        sync_card_styles()
        renpy.show_screen("rpydict_card", word=prev_w)
        renpy.restart_interaction()

    def prev_history_word():
        """返回栈顶的上一个词名（无历史时返回 None）。"""
        st = getattr(rt(), "history_stack", None)
        return st[-1] if st else None

    def on_inline_word_click(target):
        open_word_fresh(target)
        return None

    def on_card_word_click(target):
        jump_word_in_card(target)
        return None

    def current_sentence():
        import store
        raw = rt().last_raw_what or getattr(store, "_last_say_what", None)
        if not raw or not isinstance(raw, _STR_TYPES):
            return ""
        try:
            s = renpy.substitute(raw)
        except Exception:
            s = raw
        s = _strip_renpy_tags(s)
        return s.replace("\n", " ").strip()

    def tokenize(text):
        tokens = []
        pos = 0
        for m in _WORD_RE.finditer(text):
            if m.start() > pos:
                tokens.append((text[pos:m.start()], False))
            tokens.append((m.group(0), True))
            pos = m.end()
        if pos < len(text):
            tokens.append((text[pos:], False))
        return tokens

    def enter_dict_mode():
        rt().tokens = tokenize(current_sentence())

    def get_tokens():
        if not rt().tokens:
            rt().tokens = tokenize(current_sentence())
        return rt().tokens

    def record_tap_pos():
        import store
        if getattr(store.persistent, "rpydict_right_dock", False):
            rt().popup_pos = (0.98, 0.38, 1.0, 0.5)
            return
        try:
            mx, my = renpy.get_mouse_pos()
            sw = float(renpy.config.screen_width or 1280)
            sh = float(renpy.config.screen_height or 720)
            if mx <= 0 and my <= 0:
                rt().popup_pos = (0.98, 0.38, 1.0, 0.5)
                return
            fx = mx / sw
            fy = my / sh
            if fx < 0.16:
                fx = 0.16
            elif fx > 0.84:
                fx = 0.84
            if fy > 0.42:
                rt().popup_pos = (fx, max(0.06, fy - 0.045), 0.5, 1.0)
            else:
                rt().popup_pos = (fx, min(0.92, fy + 0.045), 0.5, 0.0)
        except Exception:
            rt().popup_pos = (0.98, 0.38, 1.0, 0.5)

    def get_popup_pos():
        import store
        if getattr(store.persistent, "rpydict_right_dock", False):
            return (0.98, 0.38, 1.0, 0.5)
        return getattr(rt(), "popup_pos", None) or (0.98, 0.38, 1.0, 0.5)


    # =========================================================================
    # [EXPERIMENTAL #3 - 字号档位与透明度档位调节]
    # =========================================================================

    def _font_step_idx():
        import store
        idx = getattr(store.persistent, "rpydict_font_step", None)
        if idx is None or not isinstance(idx, int):
            idx = 1  # 默认 Default 档 (offset=0)
            store.persistent.rpydict_font_step = idx
        return max(0, min(len(FONT_STEPS) - 1, idx))

    def font_delta():
        return FONT_STEPS[_font_step_idx()][1]

    def font_label():
        return FONT_STEPS[_font_step_idx()][0]

    def step_font(delta):
        import store
        store.persistent.rpydict_font_step = max(0, min(len(FONT_STEPS) - 1, _font_step_idx() + delta))
        sync_card_styles()
        renpy.restart_interaction()

    def _alpha_step_idx():
        import store
        idx = getattr(store.persistent, "rpydict_alpha_step", None)
        if idx is None or not isinstance(idx, int):
            idx = 2  # 默认 65%
            store.persistent.rpydict_alpha_step = idx
        return max(0, min(len(ALPHA_STEPS) - 1, idx))

    def alpha_label():
        return ALPHA_STEPS[_alpha_step_idx()][0]

    def bubble_bg():
        return ALPHA_STEPS[_alpha_step_idx()][1]

    def tokens_bg():
        return ALPHA_STEPS[_alpha_step_idx()][2]

    def step_alpha(delta):
        import store
        store.persistent.rpydict_alpha_step = max(0, min(len(ALPHA_STEPS) - 1, _alpha_step_idx() + delta))
        renpy.restart_interaction()

    def sync_card_styles():
        try:
            import store
            d = font_delta()
            store.style.rpydict_card_def.size = 16 + d
            store.style.rpydict_card_ex.size = 13 + d
        except Exception:
            pass


    def trigger_visible():
        try:
            if not renpy.get_screen("say"):
                return False
            for sn in ("game_menu", "main_menu", "rpydict_tokens", "rpydict_card", "rpydict_setup"):
                if renpy.get_screen(sn):
                    return False
        except Exception:
            return False
        return True

    def source_ready():
        return rt().adapter is not None or ensure_source()

    def trigger_align():
        import store
        c = store.persistent.rpydict_btn_corner or 0
        return [(0.015, 0.025), (0.985, 0.025), (0.015, 0.975), (0.985, 0.975)][c % 4]

    def cycle_corner():
        import store
        store.persistent.rpydict_btn_corner = ((store.persistent.rpydict_btn_corner or 0) + 1) % 4
        renpy.restart_interaction()

    def toggle_inline_mode():
        import store
        store.persistent.rpydict_inline_mode = not bool(store.persistent.rpydict_inline_mode)
        renpy.restart_interaction()

    def toggle_recursive_mode():
        import store
        store.persistent.rpydict_recursive_mode = not bool(store.persistent.rpydict_recursive_mode)
        renpy.restart_interaction()

    def toggle_right_dock():
        import store
        store.persistent.rpydict_right_dock = not bool(store.persistent.rpydict_right_dock)
        renpy.restart_interaction()

    def toggle_auto_height():
        import store
        store.persistent.rpydict_auto_height = not bool(store.persistent.rpydict_auto_height)
        renpy.restart_interaction()

    def setup_info():
        import store
        rescan_sources()
        granted = _android_hook("android_all_files_granted") if renpy.android else None
        sdk = _android_hook("android_sdk_int") if renpy.android else None
        return {
            "bases": candidate_base_dirs(),
            "sources": builtins.list(rt().sources),
            "active": rt().source_name,
            "selected": store.persistent.rpydict_source,
            "error": rt().last_error,
            "scan_errors": builtins.list(getattr(rt(), "scan_errors", [])),
            "android_sdk": sdk,
            "all_files_granted": bool(granted) if granted is not None else None,
        }


init python in _rpydict:

    def _android_activity():
        try:
            from jnius import autoclass
            return autoclass("org.renpy.android.PythonSDLActivity").mActivity
        except Exception:
            return None

    def android_sdk_int():
        try:
            from jnius import autoclass
            return int(autoclass("android.os.Build$VERSION").SDK_INT)
        except Exception:
            return None

    def android_all_files_granted():
        try:
            from jnius import autoclass
            if int(autoclass("android.os.Build$VERSION").SDK_INT) < 30:
                return True
            return bool(autoclass("android.os.Environment").isExternalStorageManager())
        except Exception:
            return False

    def android_request_all_files():
        try:
            from jnius import autoclass
            act = _android_activity()
            if act is None:
                rt().last_error = "Cannot open settings: Android activity is unavailable."
                renpy.restart_interaction()
                return False
            sdk = android_sdk_int()
            if sdk is not None and sdk < 30:
                rt().last_error = "This Android version has no 'All files access' switch. Use the app-specific folder shown below."
                renpy.restart_interaction()
                return False
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            package_uri = Uri.parse("package:" + act.getPackageName())
            try:
                intent = Intent("android.settings.MANAGE_APP_ALL_FILES_ACCESS_PERMISSION", package_uri)
                act.startActivity(intent)
                rt().last_error = None
                return True
            except Exception as permission_error:
                try:
                    intent = Intent("android.settings.APPLICATION_DETAILS_SETTINGS", package_uri)
                    act.startActivity(intent)
                    rt().last_error = "Could not open the all-files permission page (%s); opened app settings instead." % permission_error
                    renpy.restart_interaction()
                    return True
                except Exception as settings_error:
                    rt().last_error = "Could not open Android settings: %s" % settings_error
                    renpy.restart_interaction()
                    return False
        except Exception as e:
            rt().last_error = "Could not open Android settings: %s" % e
            renpy.restart_interaction()
            return False

    def android_ext_files_dir():
        try:
            act = _android_activity()
            if act is None:
                return None
            f = act.getExternalFilesDir(None)
            if f is None:
                return None
            d = os.path.join(str(f.getAbsolutePath()), "rpydict")
            if not os.path.isdir(d):
                os.makedirs(d)
            return d
        except Exception:
            return None


# =============================================================================
# 样式与全局挂载：接管 config.say_menu_text_filter 与 config.hyperlink_handlers
# =============================================================================

style rpydict_inline_word is say_dialogue:
    hover_underline True

style rpydict_card_def is default:
    color "#f2f6fa"
    hover_color "#78b4f0"
    hover_underline True
    size 16

style rpydict_card_ex is default:
    color "#a0b0c0"
    hover_color "#78b4f0"
    italic True
    hover_underline True
    size 13

init 999 python:

    _rpydict.sync_card_styles()

    if "rpydict_trigger" not in config.overlay_screens:
        config.overlay_screens.append("rpydict_trigger")

    config.hyperlink_handlers["rpydict"] = _rpydict.on_inline_word_click
    config.hyperlink_handlers["rpydict_def"] = _rpydict.on_card_word_click
    config.hyperlink_handlers["rpydict_ex"] = _rpydict.on_card_word_click

    _rpydict_orig_hl_funcs = style.default.hyperlink_functions
    _rpydict_orig_styler = _rpydict_orig_hl_funcs[0] if _rpydict_orig_hl_funcs else None
    _rpydict_orig_clicked = _rpydict_orig_hl_funcs[1] if (_rpydict_orig_hl_funcs and len(_rpydict_orig_hl_funcs) > 1) else None
    _rpydict_orig_focus = _rpydict_orig_hl_funcs[2] if (_rpydict_orig_hl_funcs and len(_rpydict_orig_hl_funcs) > 2) else None

    def _rpydict_hyperlink_styler(target):
        if isinstance(target, str):
            if target.startswith("rpydict:"):
                return style.rpydict_inline_word
            if target.startswith("rpydict_def:"):
                return style.rpydict_card_def
            if target.startswith("rpydict_ex:"):
                return style.rpydict_card_ex
        if callable(_rpydict_orig_styler):
            return _rpydict_orig_styler(target)
        return style.hyperlink_text

    hyperlink_styler = _rpydict_hyperlink_styler
    style.default.hyperlink_functions = (_rpydict_hyperlink_styler, _rpydict_orig_clicked, _rpydict_orig_focus)
    try:
        style.say_dialogue.hyperlink_functions = (_rpydict_hyperlink_styler, _rpydict_orig_clicked, _rpydict_orig_focus)
    except Exception:
        pass
    try:
        style.rpydict_card_def.hyperlink_functions = (_rpydict_hyperlink_styler, _rpydict_orig_clicked, _rpydict_orig_focus)
        style.rpydict_card_ex.hyperlink_functions = (_rpydict_hyperlink_styler, _rpydict_orig_clicked, _rpydict_orig_focus)
    except Exception:
        pass

    _rpydict_prev_text_filter = config.say_menu_text_filter

    def _rpydict_say_menu_text_filter(text):
        if callable(_rpydict_prev_text_filter):
            text = _rpydict_prev_text_filter(text)
        if not isinstance(text, str) or not text:
            return text
        if "{a=rpydict:" not in text:
            _rpydict.rt().last_raw_what = text
        if persistent.rpydict_inline_mode:
            return _rpydict.inject_inline_links(text)
        return text

    config.say_menu_text_filter = _rpydict_say_menu_text_filter


# =============================================================================
# 界面层（支持历史栈返回、自动降级与大号跳转按钮、高度自适应/固定可切换）
# =============================================================================

screen rpydict_card_body(_e, _d):
    if _e is None:
        text "Not in the dictionary." size (15 + _d) color "#d88888"
    else:
        $ _has_senses = bool(_e.get("senses"))
        $ _has_raw = bool(_e.get("raw"))
        $ _has_all = bool(_e.get("all_columns"))

        if _has_senses:
            for _s in _e["senses"]:
                vbox:
                    spacing 2
                    hbox:
                        spacing 6
                        yalign 0.0
                        if _s.get("pos"):
                            text _rpydict.esc_text(_s["pos"]) substitute False size (12 + _d) color "#78b4f0" yalign 0.15
                        if _s.get("definition"):
                            text _rpydict.link_card_text(_s["definition"], "rpydict_def") substitute False size (16 + _d) color "#f2f6fa"
                    if _s.get("example"):
                        text _rpydict.link_card_text(_s["example"], "rpydict_ex") substitute False size (13 + _d) color "#a0b0c0" italic True
                    if _s.get("synonyms"):
                        hbox:
                            box_wrap True
                            spacing 6
                            text "syn:" size (11 + _d) color "#8090a0" yalign 0.2
                            for _syn in _s["synonyms"]:
                                textbutton _rpydict.esc_btn(_syn):
                                    action Function(_rpydict.jump_word_in_card, _syn)
                                    background None
                                    hover_background "#34507066"
                                    padding (3, 1)
                                    text_size (12 + _d)
                                    text_color "#98c0e8"
                                    text_hover_color "#ffffff"

        if _has_raw:
            for _rc, _rv in _e["raw"]:
                hbox:
                    spacing 6
                    text _rpydict.esc_text(_rc) substitute False size (12 + _d) color "#78b4f0" yalign 0.15
                    text _rpydict.link_card_text(_rv, "rpydict_def") substitute False size (15 + _d) color "#eef2f8"

        if not _has_senses and not _has_raw:
            if _has_all:
                for _ac, _av in _e["all_columns"]:
                    hbox:
                        spacing 6
                        text _rpydict.esc_text(_ac) substitute False size (12 + _d) color "#78b4f0" yalign 0.15
                        text _rpydict.link_card_text(_av, "rpydict_def") substitute False size (15 + _d) color "#eef2f8"
            else:
                text "No definition found." size (15 + _d) color "#d88888"


screen rpydict_trigger():
    zorder 150
    if _rpydict.trigger_visible():
        $ _ta = _rpydict.trigger_align()
        # TODO (Roadmap #1 - 手势召回): 后续可支持隐藏此按钮，改用屏幕边缘滑动/多指手势唤出查词与设置面板。
        # TODO (Roadmap #2 - 自由拖动): 后续可将此 frame 包装进 drag 组件并持久化坐标，支持任意拖动位置。
        frame:
            xalign _ta[0]
            yalign _ta[1]
            background "#00000048"
            padding (8, 3)
            textbutton "D":
                action If(_rpydict.source_ready(),
                          [Function(_rpydict.enter_dict_mode), Show("rpydict_tokens")],
                          Show("rpydict_setup"))
                alternate Function(_rpydict.cycle_corner)
                background None
                text_size 15
                text_color "#ffffff78"
                text_hover_color "#ffffffdd"


screen rpydict_tokens():
    modal True
    zorder 200
    key "K_ESCAPE" action Hide("rpydict_tokens")

    button:
        xfill True
        yfill True
        background None
        action Hide("rpydict_tokens")

    $ _d = _rpydict.font_delta()

    frame:
        xalign 0.5
        yalign 0.28
        xminimum 240
        xmaximum 0.58
        background _rpydict.tokens_bg()
        padding (18, 14)
        has vbox
        spacing 10
        hbox:
            spacing 18
            text "SENTENCE" size 14 color "#8a98a8" yalign 0.5
            textbutton "⚙" action [Hide("rpydict_tokens"), Show("rpydict_setup")] text_size 16 text_color "#a8c0e0" text_hover_color "#ffffff"
            textbutton "X" action Hide("rpydict_tokens") text_size 16 text_color "#b0b0b0" text_hover_color "#ffffff"

        if _rpydict.get_tokens():
            hbox:
                box_wrap True
                box_wrap_spacing 8
                spacing 6
                for _t in _rpydict.get_tokens():
                    if _t[1]:
                        textbutton _rpydict.esc_btn(_t[0]):
                            action Function(_rpydict.open_word_fresh, _t[0])
                            background None
                            hover_background "#34507066"
                            padding (3, 1)
                            text_size (20 + _d)
                            text_color "#f0f4f8"
                            text_hover_color "#78b4f0"
                    else:
                        text _rpydict.esc_text(_t[0]) substitute False size (20 + _d) color "#9098a8"
        else:
            text "No sentence to look up right now." size (16 + _d) color "#9a9a9a"


screen rpydict_card(word):
    modal True
    zorder 300
    key "K_ESCAPE" action Hide("rpydict_card")

    button:
        xfill True
        yfill True
        background None
        action Hide("rpydict_card")

    $ _e = _rpydict.card_data(word)
    $ _pos = _rpydict.get_popup_pos()
    $ _d = _rpydict.font_delta()
    $ _scroll = _rpydict.needs_scroll(_e)
    $ _prev_w = _rpydict.prev_history_word()

    frame:
        xpos _pos[0]
        ypos _pos[1]
        xanchor _pos[2]
        yanchor _pos[3]
        xminimum 180
        xmaximum 0.27
        background _rpydict.bubble_bg()
        padding (14, 10)
        has vbox
        spacing 5

        # [EXPERIMENTAL #2 - 轻量历史栈返回按钮]：当在气泡内做过递归跳转时，顶部显示 <- 上一词 按钮
        if _prev_w:
            textbutton ("<- " + _rpydict.esc_btn(_prev_w)):
                action Function(_rpydict.pop_word_history)
                background "#22344acc"
                hover_background "#345478ee"
                padding (8, 3)
                text_size (13 + _d)
                text_color "#98c8ff"
                text_hover_color "#ffffff"

        hbox:
            spacing 8
            # 主标题直接展示词典中实际命中的词条名（如复数 places 降级后直接展示 place）
            text _rpydict.esc_text(_e["word"] if _e is not None else word) substitute False size (20 + _d) bold True color "#ffffff"

            if _e is not None and _e.get("ipa"):
                text _rpydict.esc_text(_e["ipa"]) substitute False font "DejaVuSans.ttf" size (14 + _d) color "#98c0f0" yalign 0.6

        # [调试备选] 如需排查某个原型词是从哪个变形词降级来的，可取消下面两行注释（显示如: from places）：
        # if _e is not None and _e.get("redirected") and _e.get("queried_word") and _e["queried_word"].lower() != (_e.get("word") or "").lower():
        #     text ("from " + _rpydict.esc_text(_e["queried_word"])) substitute False size (12 + _d) color "#88a0b8"

        # [EXPERIMENTAL #1 - 自适应内容高度 vs 固定高度]
        if _scroll:
            viewport:
                draggable True
                mousewheel True
                scrollbars None
                xfill False
                yfill (not persistent.rpydict_auto_height)
                ymaximum 0.36
                has vbox
                spacing 6
                use rpydict_card_body(_e, _d)
        else:
            vbox:
                spacing 6
                use rpydict_card_body(_e, _d)

        # 当词条既有自身释义又有原型关联词时，展示加大加宽、好点选的 see: 跳转按钮
        if _e is not None and _e.get("lemma"):
            textbutton ("see: " + _rpydict.esc_btn(_e["lemma"])):
                action Function(_rpydict.jump_word_in_card, _e["lemma"])
                background "#22344acc"
                hover_background "#345478ee"
                padding (10, 5)
                text_size (16 + _d)
                text_color "#8cc4ff"
                text_hover_color "#ffffff"


screen rpydict_setup():
    modal True
    zorder 250
    add "#000000b8"
    key "K_ESCAPE" action Hide("rpydict_setup")

    button:
        xfill True
        yfill True
        background None
        action Hide("rpydict_setup")

    $ _info = _rpydict.setup_info()

    frame:
        xalign 0.5
        yalign 0.5
        xmaximum 0.9
        background "#101010f4"
        padding (24, 20)
        has vbox
        spacing 12
        hbox:
            xfill True
            text ("Dictionary Setup (" + _rpydict.MOD_VERSION + ")") substitute False size 22 bold True
            textbutton "X" action Hide("rpydict_setup") xalign 1.0 text_size 18

        # 第一行：交互与位置开关
        hbox:
            box_wrap True
            box_wrap_spacing 8
            spacing 16
            text "Tap dialogue:" size 15 color "#a0a0a0" yalign 0.5
            textbutton ("ON" if persistent.rpydict_inline_mode else "OFF"):
                action Function(_rpydict.toggle_inline_mode)
                text_size 15
                text_color ("#60d080" if persistent.rpydict_inline_mode else "#c07070")
                text_hover_color "#ffffff"

            text "Recursive tap:" size 15 color "#a0a0a0" yalign 0.5
            textbutton ("ON" if persistent.rpydict_recursive_mode else "OFF"):
                action Function(_rpydict.toggle_recursive_mode)
                text_size 15
                text_color ("#60d080" if persistent.rpydict_recursive_mode else "#c07070")
                text_hover_color "#ffffff"

            text "Position:" size 15 color "#a0a0a0" yalign 0.5
            textbutton ("Right Side" if persistent.rpydict_right_dock else "Near Word"):
                action Function(_rpydict.toggle_right_dock)
                text_size 15
                text_color "#78b4f0"
                text_hover_color "#ffffff"

            text "Height:" size 15 color "#a0a0a0" yalign 0.5
            textbutton ("Auto Fit" if persistent.rpydict_auto_height else "Fixed"):
                action Function(_rpydict.toggle_auto_height)
                text_size 15
                text_color "#78b4f0"
                text_hover_color "#ffffff"

        # 第二行：字号档位与透明度档位调节（注意：在 Ren'Py 中单方括号 [...] 会触发变量插值求值，因此按钮统一用胶囊底色的 - / +）
        hbox:
            box_wrap True
            box_wrap_spacing 8
            spacing 12
            text "Font Size:" size 15 color "#a0a0a0" yalign 0.5
            textbutton "-" action Function(_rpydict.step_font, -1) background "#22344acc" hover_background "#345478ee" padding (10, 2) text_size 16 text_color "#78b4f0" text_hover_color "#ffffff"
            text _rpydict.font_label() substitute False size 16 color "#ffffff" bold True yalign 0.5
            textbutton "+" action Function(_rpydict.step_font, 1) background "#22344acc" hover_background "#345478ee" padding (10, 2) text_size 16 text_color "#78b4f0" text_hover_color "#ffffff"

            text "   Opacity:" size 15 color "#a0a0a0" yalign 0.5
            textbutton "-" action Function(_rpydict.step_alpha, -1) background "#22344acc" hover_background "#345478ee" padding (10, 2) text_size 16 text_color "#78b4f0" text_hover_color "#ffffff"
            text _rpydict.alpha_label() substitute False size 16 color "#ffffff" bold True yalign 0.5
            textbutton "+" action Function(_rpydict.step_alpha, 1) background "#22344acc" hover_background "#345478ee" padding (10, 2) text_size 16 text_color "#78b4f0" text_hover_color "#ffffff"

        if not _info["sources"]:
            text "No dictionary (.db) found.\nPut it in one of these places:" size 18 color "#c0c0c0"
            for _d in _info["bases"]:
                text _rpydict.esc_text(_d) substitute False size 15 color "#909090"
            if renpy.android and _info["android_sdk"] is not None and _info["android_sdk"] >= 30 and not _info["all_files_granted"]:
                text "Android 11+: if shared folders are blocked, grant 'All files access', return here, then rescan." size 16 color "#c0c0c0"
                hbox:
                    spacing 12
                    textbutton "Open permission settings" action Function(_rpydict.android_request_all_files):
                        text_size 16
                    textbutton "Rescan" action Function(_rpydict.rescan_and_restart):
                        text_size 16
            elif renpy.android and _info["android_sdk"] is not None and _info["android_sdk"] < 30:
                text "This Android version has no 'All files access' switch. Try the app-specific rpydict folder listed above; it does not need that permission." size 16 color "#c0c0c0"
                textbutton "Rescan" action Function(_rpydict.rescan_and_restart):
                    text_size 16
            elif renpy.android:
                text "No dictionary was found in the folders above. Check the scan details below, then rescan." size 16 color "#c0c0c0"
                textbutton "Rescan" action Function(_rpydict.rescan_and_restart):
                    text_size 16
        else:
            text "Available dictionaries (tap to switch):" size 16 color "#909090"
            for _n, _p in _info["sources"]:
                hbox:
                    spacing 8
                    if _n == _info["active"]:
                        text _rpydict.esc_text(_n) substitute False size 18 color "#7aa0d0" bold True
                        text "(active)" size 14 color "#7aa0d0" yalign 0.2
                    else:
                        textbutton _rpydict.esc_btn(_n) action Function(_rpydict.select_source, _n):
                            text_size 18

        if _info["error"]:
            text ("Error: " + _rpydict.esc_text(_info["error"])) substitute False size 14 color "#e08080"
        if _info["scan_errors"]:
            text "Scan details (also written to the Ren'Py log):" size 14 color "#c0c0c0"
            for _scan_error in _info["scan_errors"][:3]:
                text _rpydict.esc_text(_scan_error) substitute False size 13 color "#c07070"

        hbox:
            spacing 16
            textbutton "Rescan" action Function(_rpydict.rescan_and_restart):
                text_size 16
            textbutton "Close" action Hide("rpydict_setup"):
                text_size 16