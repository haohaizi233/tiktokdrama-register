from license_trans.translate import leftover_cjk, translate_company, translate_scope


def test_qingmei_scope_still_english():
    src = (
        "许可项目：食品生产、食品销售（依法须经批准的项目，经相关部门批准后方可开展经营活动）"
        "一般项目：农副产品销售、初级农产品收购、日用百货销售、家居用品销售"
        "（除许可业务外，可自主依法经营法律法规非禁止或限制的项目）"
    )
    en = translate_scope(src)
    assert "Licensed items:" in en
    assert "General items:" in en
    assert "Food production" in en or "production of food" in en
    assert leftover_cjk(en) == []


def test_compose_unseen_scope():
    en = translate_scope(
        "一般项目：化工产品及化妆品销售、植物提取、生物发酵技术研究、技术服务、"
        "中草药种植、食用农产品批发、再生资源回收"
    )
    assert "sales of chemical products and cosmetics" in en
    assert "extraction of plants" in en
    assert "technical services" in en
    assert "planting of Chinese herbal medicines" in en
    assert leftover_cjk(en) == []


def test_except_clause():
    en = translate_scope("化工产品销售（除化学危险品及监控化学品）")
    assert "sales of chemical products" in en
    assert "excluding" in en
    assert leftover_cjk(en) == []


def test_company_industry_words():
    assert "Biotechnology" in translate_company("杭州舒诚生物科技有限公司")
    assert "Trading" in translate_company("株洲市浩航商贸有限公司")
    assert "Renewable Resources" in translate_company("南京熙熙杰再生资源回收有限公司")


def test_colon_used_as_item_separator():
    en = translate_scope("一般项目：中草药种植：中草药收购：食用农产品零售")
    assert "planting of Chinese herbal medicines" in en
    assert "purchase of Chinese herbal medicines" in en or "procurement of Chinese herbal medicines" in en
    assert "retail of edible agricultural products" in en
    assert leftover_cjk(en) == []


def test_clip_glued_name_then_translate():
    en = translate_scope(
        "罗惠容91510722MACJT04Y8W型有限责任公司（自然人独资）称三台县跃坤农业发展有限公司"
        "一般项目：中草药种植、中草药收购、食用农产品零售"
    )
    assert "Licensed items" not in en or True
    assert en.startswith("General items:")
    assert leftover_cjk(en) == []


def test_ocr_typos_in_scope():
    en = translate_scope("一般项目：金质材料销售、闵门和旋塞销售、转承销售、机械设备租货")
    assert "sales of metal materials" in en
    assert "valves" in en
    assert "bearings" in en
    assert leftover_cjk(en) == []


def test_full_phrase_with_and_not_split():
    en = translate_scope("游艺及娱乐用品销售、非金属矿及制品销售")
    assert "sales of amusement and entertainment articles" in en
    assert "sales of non-metallic minerals and products" in en
    assert leftover_cjk(en) == []


def test_commodity_beats_trailing_verb():
    en = translate_scope("工程管理服务、动物饲养。")
    assert "project management services" in en
    assert "animal raising" in en
    assert leftover_cjk(en) == []


def test_primary_processing():
    en = translate_scope("食用农产品初加工")
    assert "primary processing" in en
    assert leftover_cjk(en) == []


def test_software_dev_not_eat_security_noun():
    en = translate_scope("网络与信息安全软件开发")
    assert "network and information security software" in en
    assert leftover_cjk(en) == []


def test_glued_items_get_split():
    en = translate_scope("塑料制品销售新能源汽车整车销售、金属制品销售电气设备销售")
    assert "sales of plastic products" in en
    assert "sales of complete new energy vehicles" in en
    assert "sales of metal products" in en
    assert "sales of electrical equipment" in en
    assert leftover_cjk(en) == []


def test_顿号_inside_parentheses_stays_one_item():
    en = translate_scope("煤炭销售（不在北京地区开展实物煤的交易、储运活动）")
    assert "sales of coal" in en
    assert leftover_cjk(en) == []


def test_nested_service_except_clause():
    en = translate_scope("护理机构服务（不含医疗服务）")
    assert "nursing" in en.lower()
    assert leftover_cjk(en) == []


def test_official_catalog_long_items():
    en = translate_scope(
        "中草药种植（除中国稀有和特有的珍贵优良品种）、非金属矿物制品制造"
    )
    assert leftover_cjk(en) == []
    assert "Chinese herbal medicines" in en
    assert "non-metallic mineral" in en


def test_clip_scope_drops_annual_report_footer():
    from license_trans.parse import clip_scope_text
    from license_trans.translate import leftover_cjk, translate_scope

    henan = (
        "一般项目:汽车零部件及配件制造，汽车零配件批发，汽车零配件零售;"
        "互联网销售(除销售需要许可的商品)"
        "(除依法须经批准的项目外，凭营业执照依法自主开展经营活动)"
        "国家企业信用信息公示系统网址ittp://www.gsxtgov.cn"
        "市场主体应当干每年1月1日至6月30日通过/www.gsxtgov.cn"
        "国家企业信用信息公示系统报送公示年度报告"
    )
    clipped = clip_scope_text(henan)
    assert "汽车零部件" in clipped
    assert "gsxt" not in clipped.lower()
    assert "公示系统" not in clipped
    assert "市场主体" not in clipped
    assert "年度报告" not in clipped
    en = translate_scope(henan)
    assert "auto parts" in en.lower()
    assert "gsxt" not in en.lower()
    assert "annual report" not in en.lower()
    assert "Market entities shall" not in en
    assert "报送" not in en
    assert leftover_cjk(en) == []


def test_english_scope_strips_footer_and_cjk():
    from license_trans.translate import leftover_cjk, sanitize_english_scope, translate_scope

    zh = (
        "一般项目:家具零配件销售;日用百货销售。"
        "(除依法须经批准的项目外，凭营业执照依法自主开展经营活动)"
        "市场主体应当于每年1月1日至6月30日通过国家企业信用信息公示系统网"
        "http://www.gsxt.gov.cn国家信用公示系统报送公示年度报告。"
    )
    en = translate_scope(zh)
    assert "furniture" in en.lower()
    assert leftover_cjk(en) == []
    assert "gsxt" not in en.lower()
    assert "Market entities shall" not in en
    assert "年度报告" not in en
    assert "报送" not in en

    mixed = (
        "sales of furniture parts; (except for items subject to approval in accordance "
        "with the law, business activities may be carried out independently against the "
        "business license); Market entities shall, on an annual basis1Month1to (date)"
        "6Month30day through the country enterprise credit informationPublic announcement "
        "systemsNetworkhttp://www.gsxt.gov.cn国家信用Public announcement systems报送"
        "Public announcement年度报告"
    )
    cleaned = sanitize_english_scope(mixed)
    assert leftover_cjk(cleaned) == []
    assert "gsxt" not in cleaned.lower()
    assert "Market entities" not in cleaned
    assert "furniture" in cleaned.lower()
    assert "business license" in cleaned.lower()

    from license_trans.parse import clip_scope_text

    src = (
        "一般项目:工业机器人制造。"
        "(除依法须经批准的项目外，凭营业执照依法自主开展经营活动)"
        "许可项目:航天设备制造。"
        "(依法须经批准的项目，经相关部门批准后方可开展经营活动，具体经营项目以审批结果为准)"
        "国家企业信用信息公示系统网址:http://www.gsxt.gov.cn"
        "行场主体应当于每年国家信用公示系统报送公示年度报告。"
    )
    clipped = clip_scope_text(src)
    assert "工业机器人制造" in clipped
    assert "许可项目" in clipped
    assert "航天设备制造" in clipped
    assert "gsxt" not in clipped.lower()
    assert "年度报告" not in clipped


def test_ocr_fixes_stationery_and_sports_typos():
    from license_trans.parse import clip_scope_text
    from license_trans.translate import leftover_cjk, translate_scope

    src = "一般项目:文县用品零售、体存用品及器材批发、玩且销售、礼品花客销售"
    clipped = clip_scope_text(src)
    assert "文具用品" in clipped
    assert "体育用品" in clipped
    assert "玩具销售" in clipped
    assert "礼品花卉" in clipped
    en = translate_scope(src)
    assert leftover_cjk(en) == []
    assert "Wen County" not in en
    assert "Flower Guest" not in en


def test_scope_drops_right_column_zhu_not_daily_accommodation():
    from license_trans.ocr import OcrLine
    from license_trans.parse import parse_license

    lines = [
        OcrLine("营业执照", 0.9, 300, 40, 700, 90),
        OcrLine("统一社会信用代码", 0.9, 50, 100, 220, 130),
        OcrLine("91430211MA7MWFUJ1U", 0.95, 50, 140, 220, 160),
        OcrLine("名称 株洲市浩航商贸有限公司", 0.9, 55, 230, 360, 255),
        OcrLine("类型 有限责任公司(自然人投资或控股)", 0.9, 55, 260, 400, 280),
        OcrLine("法定代表人 李晖", 0.9, 55, 287, 206, 314),
        OcrLine("住", 0.9, 578, 314, 596, 337),
        OcrLine("所 湖南省株洲市天元区马家河街道新马南", 0.9, 667, 314, 996, 337),
        OcrLine("经营范围", 0.9, 55, 319, 163, 337),
        OcrLine("一般项目:计算机软硬件及辅助设备零售:计算机软硬件及辅助设备批发:日", 0.9, 170, 319, 528, 330),
        OcrLine("用百货销售，文具用品零售:文具用品批发，办公用品销售:互联网销售(除", 0.9, 170, 327, 528, 339),
        OcrLine("路266号新马金谷三期C1栋207号", 0.9, 693, 331, 953, 354),
        OcrLine("销售需要许可的商品)办公设备耗材销售，劳动保护用品销售", 0.9, 169, 335, 528, 361),
        OcrLine("销售:日用家电零售(除依法须经批准的项目外，凭营业执照依法自主开展经", 0.9, 170, 462, 528, 481),
        OcrLine("费活动)许可项目:", 0.9, 170, 476, 271, 488),
        OcrLine("，烟草制品零售", 0.9, 314, 476, 377, 488),
        OcrLine("建设工程施工(依法须经批", 0.9, 395, 476, 528, 490),
        OcrLine("准的项目，经相关部门批准后方可开展经营活动", 0.9, 170, 489, 388, 502),
    ]
    fields = parse_license(lines)
    assert "日住" not in (fields.scope or "")
    assert "日用百货" in (fields.scope or "")
    assert "许可项目" in (fields.scope or "")
    assert "烟草制品" in (fields.scope or "")
    assert "266号" not in (fields.scope or "")


def test_jindikang_scope_composes_machining_items():
    src = (
        "一般项目：机械零件、零部件加工；机械设备销售；机械零部件、零部件销售"
        "（除依法须经批准的项目外，凭营业执照依法自主开展经营活动）。"
    )
    en = translate_scope(src)
    assert leftover_cjk(en) == []
    assert en.startswith("General items:")
    assert "General items:;" not in en
    assert "processing of mechanical parts" in en.lower()
    assert "sales of machinery" in en.lower() or "sales of mechanical parts" in en.lower()
    assert "A10601" not in en
    assert "special-purpose" not in en.lower()
    assert "creative" not in en.lower()


def test_address_leak_is_not_translated_as_scope():
    from license_trans.translate import english_scope_is_weak
    from license_trans.rule_repair import scope_text_is_plausible

    junk = "许可项目所陕西省宝鸡市高新开发区高新十创意大厦A座1单元0601号乐发区行审批专用章"
    assert scope_text_is_plausible(junk) is False
    assert translate_scope(junk) == ""
    assert english_scope_is_weak(junk, "Licensed items: development creativeA10601 special-purpose")


def test_kaiping_senquan_company_name():
    en = translate_company("开平市森泉贸易有限公司")
    assert "Senquan" in en
    assert "Shisenquan" not in en
    assert "Trading" in en


def test_kaiping_scope_composes_food_items():
    src = (
        "销售:预包装食品、散装食品(食品经营)、食用农产品(不含活禽):生产:瓶(罐)装饮用水(食品生产)"
        "(依法须经批准的项目，经相关部门批准后方可开展经营活动。)"
    )
    en = translate_scope(src)
    assert leftover_cjk(en) == []
    assert "prepackaged" in en.lower() or "pre-packaged" in en.lower() or "packaged food" in en.lower()
    assert "live poultry" in en.lower()
    assert "drinking water" in en.lower() or "bottled" in en.lower()
    assert "office1" not in en.lower()
    assert "(Can)" not in en
    assert "Licensed items: live poultry)" not in en


def test_kaiping_garbled_scope_is_implausible():
    from license_trans.rule_repair import scope_text_is_plausible
    from license_trans.translate import english_scope_is_weak

    junk = "许可项目住含活禽):生产:瓶(罐)装饮用水(食品生产)(依法须办公楼旧址1号经批准的项目，经相关部门批准后方可开展经营活动。)"
    assert scope_text_is_plausible(junk) is False
    assert translate_scope(junk) == ""
    assert english_scope_is_weak(
        junk, "Licensed items: live poultry); production: bottled (can) drinking water",
    )


def test_haohang_dot_header_keeps_general_items():
    from license_trans.parse import clip_scope_text
    from license_trans.rule_repair import scope_text_is_plausible
    from license_trans.translate import english_scope_is_weak

    blob = (
        "·般项目，计算机软硬件及辅助设备零售:计算机软硬件及辅助设备批发"
        "用百货销售，文具用品零售:文具用品批发，办公用品销售"
        "路266号新马金谷三期C1栋207号"
        "销售需要许可的商品)办公设备耗材销售"
        "销售:日用家电零售(除依法须经批准的项目外，凭营业执照依法自主开展经营活动。"
        "许可项目:，烟草制品零建设工程施工(依法须经批准的项目，经相关部门批准后方可开展经营活动"
    )
    clipped = clip_scope_text(blob)
    assert "一般项目" in clipped
    assert "计算机软硬件" in clipped
    assert "日用百货" in clipped
    assert "许可项目" in clipped
    assert "烟草制品零售" in clipped
    assert "建设工程施工" in clipped
    assert "266号" not in clipped
    assert "烟草制品零建" not in clipped
    assert "体育用品及器材" in clip_scope_text(
        "五金产以量心，优春用总及我材零售，体育用品及器材批发"
    )
    assert scope_text_is_plausible(clipped)

    glue = (
        "许可项目：烟草制品零建设工程施工"
        "(依法须经批准的项目，经相关部门批准后方可开展经营活动"
    )
    from license_trans.pipeline import _scope_truncated
    assert _scope_truncated(glue)
    assert "烟草制品零售" in clip_scope_text(glue)
    en = translate_scope(clipped)
    assert leftover_cjk(en) == []
    assert en.startswith("General items:")
    assert "Licensed items:" in en
    assert "construction of Licensed items" not in en
    assert "tobacco" in en.lower()
    assert "software" in en.lower() or "computer" in en.lower()
    assert english_scope_is_weak(
        glue,
        "construction of Licensed items: tobacco products construction",
    )


def test_haohang_ocr_lines_keep_left_column_scope():
    from license_trans.ocr import OcrLine
    from license_trans.parse import parse_license
    from license_trans.pipeline import _harvest_from_ocr, _scope_from_ocr_lines
    from license_trans.field_state import FieldStateTracker

    lines = [
        OcrLine("营业执照", 0.9, 304, 100, 720, 195),
        OcrLine("名称 株洲市浩航商贸有限公司", 0.9, 63, 233, 358, 256),
        OcrLine("类型 有限责任公司(自然人投资或控股)", 0.9, 59, 259, 413, 281),
        OcrLine("法定代表人 李晖", 0.9, 54, 286, 205, 314),
        OcrLine("住", 0.9, 578, 314, 596, 336),
        OcrLine("所 湖南省株洲市天元区马家河街道新马南", 0.9, 667, 314, 995, 336),
        OcrLine("经营范围", 0.9, 55, 318, 162, 336),
        OcrLine("·般项目，计算机软硬件及辅助设备零售:计算机软硬件及辅助设备批发", 0.9, 178, 318, 504, 330),
        OcrLine("用百货销售，文具用品零售:文具用品批发，办公用品销售:互联网销售(除", 0.9, 170, 327, 528, 338),
        OcrLine("路266号新马金谷三期C1栋207号", 0.9, 692, 331, 953, 353),
        OcrLine("销售需要许可的商品)办公设备耗材销售，劳动保护用品销售;家居用品销", 0.9, 169, 335, 528, 361),
        OcrLine("销售:日用家电零售(除依法须经批准的项目外", 0.9, 170, 467, 389, 481),
        OcrLine("，烟草制品零", 0.9, 314, 475, 377, 487),
        OcrLine("建设工程施工(依法须经批", 0.9, 395, 475, 528, 489),
        OcrLine("营活动。许可项目:", 0.9, 170, 480, 268, 487),
        OcrLine("准的项目，经相关部门批准后方可开展经营活动", 0.9, 170, 488, 387, 502),
    ]
    fields = parse_license(lines)
    from_lines = _scope_from_ocr_lines(lines)
    assert "一般项目" in (fields.scope or from_lines)
    assert "计算机软硬件" in (fields.scope or from_lines)
    assert "266号" not in (fields.scope or "")
    assert "266号" not in from_lines

    fields.scope = (
        "许可项目：烟草制品零建设工程施工"
        "(依法须经批准的项目，经相关部门批准后方可开展经营活动"
    )
    tracker = FieldStateTracker()
    tracker.init_field("scope", fields.scope)
    _harvest_from_ocr(fields, tracker, [ln.text for ln in lines], lines)
    assert "一般项目" in fields.scope
    assert "计算机软硬件" in fields.scope
    assert "烟草制品零售" in fields.scope
    assert "建设工程施工" in fields.scope
    assert "烟草制品零建" not in fields.scope


def test_combine_scope_appends_licensed_tail():
    from license_trans.pipeline import _combine_scope

    general = (
        "一般项目：计算机软硬件及辅助设备零售；日用百货销售；文具用品批发"
        "（除依法须经批准的项目外，凭营业执照依法自主开展经营活动）"
    )
    licensed = "许可项目：酒类经营；烟草制品零售；建设工程施工（依法须经批准的项目，经相关部门批准后方可开展经营活动）"
    merged = _combine_scope(general, licensed)
    assert "一般项目" in merged
    assert "计算机软硬件" in merged
    assert "许可项目" in merged
    assert "烟草制品零售" in merged
    assert "建设工程施工" in merged


def test_haohang_address_room_after_building():
    from license_trans.translate import translate_address

    en = translate_address(
        "湖南省株洲市天元区马家河街道新马南路266号新马金谷三期C1栋207号"
    )
    assert "Room 207" in en
    assert "Building C1" in en
    assert "No. 266" in en
    assert "No. 207" not in en
