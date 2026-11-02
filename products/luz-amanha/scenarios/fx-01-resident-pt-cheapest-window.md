---
id: fx-01-resident-pt-cheapest-window
persona: resident-pt
kind: fixed
title: 'Home page on a phone: tomorrow''s date, the cheap 3-hour window and the three cheapest hours in at most 2 reads'
entry: https://luz.bymachines.ai/
inputs:
  pergunta: "a que horas amanhã ponho a máquina de lavar?"
success: 'In at most 2 fetches reports the date in the heading («Luz amanhã — DD.MM.AAAA»), the «Mais barato (3 horas
  seguidas)» window with its average €/MWh, the «3 horas mais baratas», and one sentence that the number is the OMIE
  wholesale price without taxes or grid fees (from the page''s own text). If the page says OMIE has not published and
  shows today instead, reporting that correctly is also success.'
expected_gaps:
- 'The 23:00 row of tomorrow shows «sem dados / no data» (OMIE publishes in Spanish time); the page explains it.'
- 'The €/kWh estimate with ERSE components is off; only «€/kWh energia» (a plain unit conversion) is shown, with a
  note that the estimate appears once the responsible person sets the components.'
- 'Before ≈ 14:30 UTC the page may still show today with a notice; the scenario accepts that.'
max_steps: 3
budget_usd: 0.2
version: 1
created: '2026-11-02'
baseline: null
---

Estás na cozinha, telemóvel na mão, e queres saber a que horas amanhã a luz é mais barata para pôr a máquina de
lavar. Abre https://luz.bymachines.ai/ e responde: que dia é que a página mostra, qual é a janela de 3 horas mais
barata e as 3 horas mais baratas, e o que é exatamente o número que estás a ver. Se a página disser que os preços
de amanhã ainda não saíram, diz isso.

You are the resident above. Report in English, quoting the Portuguese text as printed. Stop after max_steps
fetches or when done. Report every URL you fetched, where you stopped, and classify the outcome.
