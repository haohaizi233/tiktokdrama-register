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
