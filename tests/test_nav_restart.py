import uilayout as UL


def make(store):
    def get_prefs(chat):
        return store.setdefault(chat, {})

    def set_pref(chat, k, v):
        store.setdefault(chat, {})[k] = v
    return UL.Layout({}, [], {}, get_prefs, set_pref)


def test_back_from_subscreen_survives_restart():
    store = {}
    L = make(store)
    tok = UL.SCOPE.set("m100")
    try:
        L.visit(1, "m:home"); L.shown(1, "m:home")
        L.visit(1, "m:hub:cr"); L.shown(1, "m:hub:cr")
        L.shown(1, "m:pl:stk:")  # підекран (розділ Наліпки всередині Медійки)
    finally:
        UL.SCOPE.reset(tok)
    L2 = make(store)  # перезапуск бота: пам'ять порожня, лишились налаштування
    tok = UL.SCOPE.set("m100")
    try:
        assert L2.back(1) == "m:hub:cr"  # «Назад» — до Медійки, а не одразу додому
        L2.shown(1, "m:hub:cr")  # бот показав Медійку
        assert L2.back(1) == "m:home"
    finally:
        UL.SCOPE.reset(tok)
