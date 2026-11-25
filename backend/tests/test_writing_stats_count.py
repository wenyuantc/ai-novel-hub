from bisect import bisect_left
from random import Random

import pytest

from app.services.writing_stats import count_added_words


@pytest.mark.parametrize(("before", "after", "added"), [
    ("", "山 风。", 3),
    ("上乙\n乙\n乙\n尾", "乙\n上乙\n乙\n乙\n尾", 1),
    ("上乙\n乙\n乙\n尾", "乙\n乙尾", 0),
    ("\n".join("甲甲甲甲甲甲甲"), "\n".join("甲乙甲甲甲甲乙"), 2),
    ("零" + "甲乙" * 24_000 + "尾", "丙" + "甲乙" * 24_000 + "丁", 2),
    ("首" + "甲乙丙丁" * 20_000 + "尾", "短头" + "甲乙丙丁" * 20_000 + "壬癸尾", 4),
    ("首" + "甲乙" * 4000 + "尾", "新" * 100 + "甲乙" * 4000 + "终", 101),
    ("甲" + "甲乙" * 4000 + "乙", "乙" * 100 + "甲乙" * 4000 + "甲", 101),
    ("春风吹过山岗", "暖风吹过山坡", 2),
    ("甲" * 100, "乙" * 100, 100),
    ("春风🙂", "春风", 0),
    ("海边", " 海\n边\u2002", 0),
    ("白" * 25_000, "白" * 10 + "黑" + "白" * 24_978 + "黑" + "白" * 10, 2),
    ("甲乙" * 40_000, "火水" * 40_000, 80_000),
    ("甲乙丙\n不动\n乙", "甲丙\n不动\n乙乙", 1),
])
def test_count_added_words(before, after, added):
    assert count_added_words(before, after) == added


def test_insert_paragraph_and_copyedit_every_thirtieth_character():
    before = "".join(chr(0x4e00 + index) for index in range(6000))
    edited = "".join("。" if index % 30 == 0 else char for index, char in enumerate(before))
    assert count_added_words(before, "F" * 300 + edited + "末") == 501


@pytest.mark.parametrize("length", [6000, 12_000])
def test_frequent_copyedits_in_shared_alphabet_with_large_prefix_insertion(length):
    alphabet = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"
    before = "".join(Random(1).choices(alphabet, k=length))
    edited = "".join(
        alphabet[(alphabet.index(char) + 1) % len(alphabet)] if index % 30 == 0 else char
        for index, char in enumerate(before)
    )
    assert count_added_words(before, "甲" * 300 + edited + "乙") == 301 + length // 30


def test_whole_ordinary_chapter_shuffle_matches_exact_lcs():
    # 每个字唯一时，打乱全文的最长公共子序列恰是原字符下标的最长递增子序列。
    order = list(range(6000))
    Random(42).shuffle(order)
    before = "".join(chr(0x4e00 + index) for index in range(len(order)))
    after = "".join(before[index] for index in order)
    tails = []
    for index in order:
        position = bisect_left(tails, index)
        if position == len(tails):
            tails.append(index)
        else:
            tails[position] = index
    assert count_added_words(before, after) == len(after) - len(tails)
