"""Explicit, bounded domains for the 60 currently imported MBPP tasks.

Domains are reviewed against the task, signature and reference, not inferred from
the first example's type. Bounds limit interactive runtime, not full problem scope.

v2 adds the five TACO/CodeWars hard problems; several need structured generators
(valid bowling games, well-formed polynomials) rather than plain type domains.
"""
from __future__ import annotations

from functools import lru_cache

from hypothesis import strategies as st


STRATEGY_VERSION = "mbpp-taco-domains-v2"


def _roll_char(pins: int) -> str:
    if pins == 0:
        return "0"
    if pins == 10:
        return "X"
    return str(pins)


@st.composite
def bowling_game(draw):
    """Generate a valid ten-pin bowling game string (10 space-separated frames)."""
    frames = []
    for _ in range(9):
        if draw(st.booleans()):
            frames.append("X")
        else:
            first = draw(st.integers(0, 9))
            second = draw(st.integers(0, 10 - first))
            if first + second == 10:
                frames.append(f"{_roll_char(first)}/")
            else:
                frames.append(f"{_roll_char(first)}{_roll_char(second)}")
    first = draw(st.integers(0, 10))
    if first == 10:
        second = draw(st.integers(0, 10))
        if second == 10:
            frames.append("XX" + _roll_char(draw(st.integers(0, 10))))
        else:
            third = draw(st.integers(0, 10 - second))
            tail = "/" if second + third == 10 else _roll_char(third)
            frames.append("X" + _roll_char(second) + tail)
    else:
        second = draw(st.integers(0, 10 - first))
        if first + second == 10:
            frames.append(f"{_roll_char(first)}/{_roll_char(draw(st.integers(0, 10)))}")
        else:
            frames.append(f"{_roll_char(first)}{_roll_char(second)}")
    return {"frames": " ".join(frames)}


@st.composite
def polynomial(draw):
    """Generate a well-formed polynomial string like ``-a+5ab+3a-c-2a``."""
    terms = []
    for _ in range(draw(st.integers(1, 4))):
        coeff = draw(st.integers(1, 5))
        var_len = draw(st.integers(1, 3))
        var = "".join(sorted(draw(st.lists(
            st.sampled_from("abcxyz"), min_size=var_len, max_size=var_len, unique=True
        ))))
        head = "" if coeff == 1 and draw(st.booleans()) else str(coeff)
        terms.append(head + var)
    expr = ("-" if draw(st.booleans()) else "") + terms[0]
    for term in terms[1:]:
        expr += draw(st.sampled_from("+-")) + term
    return {"poly": expr}


@st.composite
def matrix(draw, *, square=False):
    rows = draw(st.integers(1, 4))
    columns = rows if square else draw(st.integers(1, 5))
    if square:
        # Normal magic-square domain: distinct values 1..n², square shape.
        values = draw(st.permutations(tuple(range(1, rows * columns + 1))))
    else:
        values = draw(st.lists(st.integers(-20, 20), min_size=rows * columns, max_size=rows * columns))
    return [list(values[i * columns:(i + 1) * columns]) for i in range(rows)]


@st.composite
def indexed_array(draw):
    values = draw(st.lists(st.integers(-100, 100), min_size=1, max_size=20))
    return {"arr": values, "k": draw(st.integers(1, len(values)))}


@st.composite
def unique_among_pairs(draw):
    values = draw(st.lists(st.integers(-50, 50), min_size=1, max_size=12, unique=True))
    return {"arr": sorted([values[0]] + [v for v in values[1:] for _ in range(2)])}


@st.composite
def pattern_cases(draw):
    pattern = draw(st.lists(st.integers(0, 3), min_size=1, max_size=12))
    colors = [f"color{p}" for p in pattern]
    # A fresh color creates a real repeated-pattern conflict when one exists;
    # avoid the ambiguous many-patterns-to-one-color contract in the source.
    if draw(st.booleans()):
        colors[draw(st.integers(0, len(colors) - 1))] = "different"
    return {"colors": colors, "patterns": [f"p{p}" for p in pattern]}


@st.composite
def same_position_lists(draw):
    n = draw(st.integers(0, 15))
    row = st.lists(st.integers(-4, 4), min_size=n, max_size=n)
    return {"list1": draw(row), "list2": draw(row), "list3": draw(row)}


@lru_cache(maxsize=1)
def definitions():
    integer = st.integers(-100, 100)
    positive = st.integers(1, 100)
    natural = st.integers(0, 10000)
    text = st.text(alphabet="abcXYZaeiou012 _-中", max_size=30)
    word = st.text(alphabet="abcXYZaeiou", min_size=1, max_size=12)
    ints = st.lists(integer, max_size=20)
    nested = st.lists(st.lists(integer, max_size=8), max_size=8)
    pair = st.tuples(integer, integer).map(list)
    pairs = st.lists(pair, min_size=1, max_size=12)
    nonempty = st.lists(integer, min_size=1, max_size=20)
    nonempty_nested = st.lists(st.lists(integer, max_size=8), min_size=1, max_size=8)
    defs = {}

    def fields(number, domain, **kwargs):
        defs[f"mbpp_Mbpp/{number}"] = (st.fixed_dictionaries(kwargs), domain)

    def custom(number, domain, strategy):
        defs[f"mbpp_Mbpp/{number}"] = (strategy, domain)

    for number, field in [(8, "nums"), (19, "arraynums"), (66, "l"), (68, "A"),
                          (71, "nums"), (133, "nums"), (141, "nums")]:
        fields(number, "整数列表，长度 0..20，元素 -100..100；覆盖空列表、重复值和符号", **{field: ints})
    fields(9, "非空字符串，长度 1..24，字母表 abc；覆盖周期和非周期串", s=st.text("abc", min_size=1, max_size=24))
    fields(11, "字符串长度 0..30；ch 严格为一个字符", s=text, ch=st.sampled_from(list("abcXYZaeiou012 _-中")))
    fields(12, "矩形整数矩阵，1..4 行、1..5 列，元素 -20..20", M=matrix())
    fields(14, "棱柱三个长度均为正整数 1..100", l=positive, b=positive, h=positive)
    fields(17, "正整数边长 1..100", a=positive)
    fields(18, "两个字符串长度各 0..30，可含重复字符", string=text, second_string=text)
    fields(56, "非负整数 0..10000，十进制反转不带负号", n=natural)
    fields(57, "非空数字列表，长度 1..12，每项 0..9", arr=st.lists(st.integers(0, 9), min_size=1, max_size=12))
    fields(58, "整数 x/y 各 -100..100，包含零", x=integer, y=integer)
    for number in (59, 80, 86, 135):
        fields(number, "序号 n 为正整数 1..100", n=positive)
    fields(62, "非空整数列表，长度 1..20", xs=nonempty)
    fields(63, "非空数对列表，每项严格为两个整数", test_list=pairs)
    fields(64, "学科名和分数组成的二元组列表，分数 0..100，允许相同分数",
           subjectmarks=st.lists(st.tuples(word, st.integers(0, 100)).map(list), max_size=15))
    fields(67, "Bell 数序号 n=0..12，限制生成规模", n=st.integers(0, 12))
    fields(69, "整数主列表和连续子列表，长度各 0..20", l=ints, s=ints)
    fields(70, "非空外层列表，内层长度可为零或不等", Input=nonempty_nested)
    fields(72, "非负整数 n=0..10000", n=natural)
    custom(74, "等长颜色/模式序列，1..12 项；双向一一对应或重复模式冲突", pattern_cases())
    fields(75, "嵌套整数列表；除数 K 为正整数 1..20", test_list=nested, K=st.integers(1, 20))
    fields(77, "整数 n=-10000..10000", n=st.integers(-10000, 10000))
    fields(79, "非空单词，长度 1..12", s=word)
    fields(89, "整数 N=-100..100", N=integer)
    fields(90, "非空单词列表，长度 1..15", list1=st.lists(word, min_size=1, max_size=15))
    fields(91, "字符串列表及子串，允许空列表和空子串", str1=st.lists(text, max_size=10), sub_str=text)
    fields(92, "非负整数 n=0..10000000，数字不包含负号", n=st.integers(0, 10000000))
    fields(93, "整数底数 -12..12，非负整数指数 0..8", a=st.integers(-12, 12), b=st.integers(0, 8))
    fields(95, "非空外层列表，内层长度 0..8", lst=nonempty_nested)
    fields(96, "正整数 n=1..500", n=st.integers(1, 500))
    fields(97, "嵌套整数列表，允许重复和空行", list1=nested)
    fields(100, "非负整数 num=0..100000", num=st.integers(0, 100000))
    custom(101, "非空整数列表；1 <= k <= len(arr)，一基索引", indexed_array())
    fields(102, "1..6 个非空小写单词以下划线连接", word=st.lists(st.text("abcxyz", min_size=1, max_size=8), min_size=1, max_size=6).map("_".join))
    custom(103, "Eulerian n=1..8；0 <= m < n", st.integers(1, 8).flatmap(
        lambda n: st.fixed_dictionaries({"n": st.just(n), "m": st.integers(0, n - 1)})))
    fields(104, "嵌套字符串列表，外层/内层长度 0..8", input_list=st.lists(st.lists(text, max_size=8), max_size=8))
    fields(105, "严格布尔列表，长度 0..30", lst=st.lists(st.booleans(), max_size=30))
    custom(109, "非空二进制字符串，长度 1..30；n 与字符串长度一致",
           st.text("01", min_size=1, max_size=30).map(lambda s: {"s": s, "n": len(s)}))
    fields(111, "非空嵌套整数列表；交集输出忽略顺序", nestedlist=nonempty_nested)
    fields(116, "非空正整数序列，每项 1..99，长度 1..8", nums=st.lists(st.integers(1, 99), min_size=1, max_size=8))
    fields(118, "字符串长度 0..30，含连续空格", string=text)
    custom(119, "有序数组：一个值出现一次，其他互异值各出现两次", unique_among_pairs())
    fields(120, "非空整数数对列表，每项长度严格为 2", list1=pairs)
    fields(123, "亲和数求和上界 limit=1..2000，使用题目主函数", limit=st.integers(1, 2000))
    fields(125, "非空二进制字符串，长度 1..30", string=st.text("01", min_size=1, max_size=30))
    fields(127, "整数 x/y 各 -100..100", x=integer, y=integer)
    fields(128, "长度阈值 n=0..15；以空格连接的单词", n=st.integers(0, 15),
           s=st.lists(word, min_size=1, max_size=10).map(" ".join))
    custom(129, "1..4 阶正规方阵，元素为 1..n² 的排列；另含 3 阶幻方探针",
           st.one_of(st.just({"my_matrix": [[2, 7, 6], [9, 5, 1], [4, 3, 8]]}),
                     matrix(square=True).map(lambda m: {"my_matrix": m})))
    fields(131, "字符串长度 0..30，含大小写元音和辅音", str1=text)
    fields(132, "单字符序列，长度 0..30", tup1=st.lists(st.sampled_from(list("abcXYZ中 ")), max_size=30))
    fields(138, "非负整数 n=0..10000", n=natural)
    custom(142, "三条等长整数列表，长度 0..15，元素 -4..4", same_position_lists())

    taco_text = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ,.'", max_size=40)
    defs["taco_converter"] = (
        st.fixed_dictionaries({
            "n": st.integers(-1000, 1000),
            "decimals": st.integers(0, 5),
            "base": st.one_of(st.just(3.141592653589793), st.integers(2, 16)),
        }),
        "整数 n -1000..1000；decimals 0..5 与 base（π 或 2..16 整数）始终显式给出",
    )
    defs["taco_mix"] = (
        st.fixed_dictionaries({"s1": taco_text, "s2": taco_text}),
        "两条长度 0..40 的混合大小写字符串，含空格与标点",
    )
    defs["taco_count_change"] = (
        st.fixed_dictionaries({
            "money": st.integers(0, 12),
            "coins": st.lists(st.integers(1, 9), min_size=1, max_size=3, unique=True),
        }),
        "money 0..12；coins 为 1..3 个互异面额 1..9（参考为朴素递归，域刻意保持小规模）",
    )
    defs["taco_simplify"] = (
        polynomial(),
        "1..4 项多项式；系数 1..5 可省略 1；变量为 abcxyz 中 1..3 个互异字母按字母序",
    )
    defs["taco_bowling_score"] = (
        bowling_game(),
        "合法十瓶保龄球局：前 9 帧单投全中或两投和 ≤10，第 10 帧含补投规则",
    )
    return defs


def external_strategy_for(problem_id):
    return definitions()[problem_id][0]


def external_domain(problem_id):
    return definitions()[problem_id][1]
