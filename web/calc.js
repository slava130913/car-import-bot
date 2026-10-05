/* Порт calc/engine.py на JavaScript. Логика должна совпадать один в один:
   tests/test_parity.py прогоняет одни и те же кейсы через Python и Node и сравнивает. */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.CarCalc = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const FUEL_LABELS = {
    ice: "бензин / дизель",
    hybrid_par: "параллельный гибрид (считается как ДВС)",
    hybrid_seq: "последовательный гибрид (считается как электромобиль)",
    ev: "электромобиль",
  };
  const AGE_LABELS = { new: "до 3 лет", "3-5": "от 3 до 5 лет", "5+": "старше 5 лет" };

  // Python round(): banker's rounding для .5. Воспроизводим, чтобы итоги совпадали копейка в копейку.
  function pyRound(x) {
    const f = Math.floor(x);
    const diff = x - f;
    if (diff > 0.5) return f + 1;
    if (diff < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  }

  function ageCategoryFromDate(made, today) {
    today = today || new Date();
    let years = today.getFullYear() - made.getFullYear();
    const before = today.getMonth() < made.getMonth() ||
      (today.getMonth() === made.getMonth() && today.getDate() < made.getDate());
    if (before) years -= 1;
    if (years < 3) return "new";
    if (years < 5) return "3-5";
    return "5+";
  }

  function bracket(brackets, value) {
    for (const b of brackets) if (value <= b[0]) return b;
    return brackets[brackets.length - 1];
  }

  function volumeKey(cc) {
    if (cc <= 1000) return "0-1000";
    if (cc <= 2000) return "1000-2000";
    if (cc <= 3000) return "2000-3000";
    if (cc <= 3500) return "3000-3500";
    return "3500+";
  }

  const isEvScheme = (fuel) => fuel === "ev" || fuel === "hybrid_seq";

  function utilFee(rules, car) {
    const u = rules.util;
    const pref = u.preferential;
    const ev = isEvScheme(car.fuel);
    const maxHp = ev ? pref.max_hp_ev : pref.max_hp_ice;
    const ccOk = ev || car.engine_cc <= pref.max_cc;
    const isNew = car.age === "new";

    if (car.power_hp <= maxHp && ccOk) {
      return [isNew ? pref.new_rub : pref.old_rub, "льготная ставка для физлица", false];
    }
    let table;
    if (ev) {
      table = u.commercial.ev.brackets;
    } else {
      const vol = u.commercial.ice[volumeKey(car.engine_cc)];
      table = vol.brackets;
      if (table === null || table === undefined) return [null, vol.note || "ставка не подтверждена", true];
    }
    const b = bracket(table, car.power_hp);
    const amount = isNew ? b[1] : b[2];
    if (amount === null || amount === undefined) {
      return [null, "для этой мощности ставка не подтверждена, уточните у брокера", true];
    }
    if (car.power_hp > table[table.length - 1][0]) return [amount, "коммерческая ставка, верхняя граница таблицы", true];
    return [amount, "коммерческая ставка (мощность выше льготного порога)", false];
  }

  function line(key, label, mid, low, high, note) {
    return { key, label, mid, low: low === undefined ? null : low, high: high === undefined ? null : high, note: note || "" };
  }

  function customsPayments(rules, car, priceRub, eurRub) {
    const lines = [];
    if (isEvScheme(car.fuel)) {
      const s = rules.ev_scheme;
      const duty = priceRub * s.duty_pct / 100;
      const perHp = bracket(s.excise_rub_per_hp.brackets, car.power_hp)[1];
      const excise = perHp * car.power_hp;
      const vat = (priceRub + duty + excise) * s.vat_pct / 100;
      lines.push(line("duty", `Пошлина ${s.duty_pct}%`, pyRound(duty)));
      lines.push(line("excise", `Акциз (${perHp} ₽ за л.с.)`, pyRound(excise)));
      lines.push(line("vat", `НДС ${s.vat_pct}%`, pyRound(vat)));
      return lines;
    }
    const d = rules.customs_duty_individual;
    const priceEur = priceRub / eurRub;
    let dutyEur, how;
    if (car.age === "new") {
      const b = bracket(d.new_under_3y.brackets, priceEur);
      dutyEur = Math.max(priceEur * b[1] / 100, car.engine_cc * b[2]);
      how = `${b[1]}% от стоимости, но не менее ${b[2]} € за см³`;
    } else {
      const key = car.age === "3-5" ? "age_3_to_5" : "age_over_5";
      const b = bracket(d[key].brackets, car.engine_cc);
      dutyEur = car.engine_cc * b[1];
      how = `${b[1]} € за см³`;
    }
    lines.push(line("duty", "Таможенная пошлина (единая ставка для физлиц)", pyRound(dutyEur * eurRub), undefined, undefined, how));
    return lines;
  }

  function customsFee(rules, priceRub) {
    return bracket(rules.customs_fee.brackets, priceRub)[1];
  }

  function fmtCny(v) {
    return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // extras: строки компании, которая считает для клиента: [[название, сумма в рублях], ...]
  function calculate(car, rules, extras) {
    const fb = rules.fallback_rates;
    const cnyRub = car.cny_rub || fb.CNY;
    const eurRub = car.eur_rub || fb.EUR;
    const res = { lines: [], warnings: [], unverified: false, rates: { CNY: cnyRub, EUR: eurRub }, rules_version: rules.version };
    const costs = rules.costs;

    const priceRub = car.price_cny * cnyRub;
    res.lines.push(line("price", `Цена автомобиля (${fmtCny(car.price_cny)} ¥ × ${cnyRub.toFixed(2)})`, pyRound(priceRub)));

    const pa = costs.payment_agent_pct;
    res.lines.push(line("payment_agent", pa.label, pyRound(priceRub * pa.mid / 100), pyRound(priceRub * pa.low / 100), pyRound(priceRub * pa.high / 100)));

    const cs = costs.china_side;
    res.lines.push(line("china_side", cs.label, cs.mid, cs.low, cs.high));

    const lg = costs.logistics[car.route] || costs.logistics.suifenhe;
    res.lines.push(line("logistics", lg.label, lg.mid, lg.low, lg.high, `${lg.days} дней`));

    res.lines.push(...customsPayments(rules, car, priceRub, eurRub));
    res.lines.push(line("customs_fee", "Таможенный сбор за оформление", customsFee(rules, priceRub)));

    const [util, utilNote, unverified] = utilFee(rules, car);
    res.lines.push(line("util", "Утилизационный сбор", util, undefined, undefined, utilNote));
    res.unverified = res.unverified || unverified;

    for (const key of ["broker", "svh", "sbkts_glonass", "epts_registration"]) {
      const c = costs[key];
      res.lines.push(line(key, c.label, c.mid, c.low, c.high));
    }
    const dm = costs.domestic[car.destination] || costs.domestic.moscow;
    res.lines.push(line("domestic", `Доставка по России: ${dm.label}`, dm.mid, dm.low, dm.high));
    (extras || []).forEach(([label, amount], i) => {
      if (amount) res.lines.push(line(`extra${i}`, String(label), pyRound(Number(amount))));
    });

    const pref = rules.util.preferential;
    const ev = isEvScheme(car.fuel);
    const maxHp = ev ? pref.max_hp_ev : pref.max_hp_ice;
    if (car.power_hp > maxHp) {
      res.warnings.push(`Мощность ${car.power_hp} л.с. выше порога ${maxHp} л.с.: льготный утильсбор не действует, применяется коммерческая ставка. Это главная статья расходов, перепроверьте мощность по документам.`);
    }
    if (!ev && car.engine_cc > pref.max_cc) {
      res.warnings.push(`Объём ${car.engine_cc} см³ больше ${pref.max_cc} см³: льгота по утильсбору не действует.`);
    }
    if (car.power_hp <= maxHp && (ev || car.engine_cc <= pref.max_cc)) {
      res.warnings.push("Льгота действует только при ввозе одного автомобиля в год для себя и без продажи 12 месяцев.");
    }
    if (ev) res.warnings.push("Для электромобилей и последовательных гибридов мощность считается по 30-минутной мощности электромоторов.");
    if (car.fuel === "hybrid_par") res.warnings.push("Параллельный гибрид считается как ДВС, мощность берётся суммарная (ДВС + электромотор).");
    if (res.unverified) res.warnings.push("В расчёте есть неподтверждённая ставка. Итог без неё неполный.");

    const sum = (attr) => {
      let t = 0;
      for (const ln of res.lines) {
        let v = ln[attr];
        if (v === null || v === undefined) v = ln.mid;
        if (v === null || v === undefined) continue;
        t += v;
      }
      return pyRound(t);
    };
    res.total_mid = sum("mid");
    res.total_low = sum("low");
    res.total_high = sum("high");
    return res;
  }

  function formatRub(v) {
    if (v === null || v === undefined) return "не подтверждено";
    return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " ₽";
  }

  return { calculate, utilFee, ageCategoryFromDate, formatRub, FUEL_LABELS, AGE_LABELS };
});
