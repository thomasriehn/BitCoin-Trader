# Projektplan: Automatisierter Bitcoin-Handel auf Proxmox

Stand: 15. September 2026. Erstellt für Thomas auf Basis recherchierter und gegengeprüfter Quellen (siehe Abschnitt 13). Nachtrag vom selben Tag: Die Entscheidungen aus Abschnitt 12 sind getroffen (Bitvavo, 1.000 EUR Startkapital, statische IP, Proxmox VE 9.2.10 mit ZFS, USV vorhanden, Advisor lokal über vLLM). Die verbindliche Bauanleitung dafür steht in `docs/KOMPONENTEN.md`.

Dieses Dokument ist keine Rechts- oder Steuerberatung. Lass die Punkte aus Abschnitt 2 und 3 vor dem Live-Betrieb von einem Steuerberater mit Krypto-Erfahrung prüfen.

Wo eine Angabe nicht gegen eine Primärquelle geprüft werden konnte, steht "nicht abschließend verifiziert".

## 1. Kurzfassung

- **Ist das legal?** Ja. Der Handel mit ausschließlich eigenem Geld auf eigene Rechnung ist keine erlaubnispflichtige Kryptowerte-Dienstleistung (BaFin-Merkblatt vom 03.01.2025, Abschnitt II.3). Bedingungen: nur eigenes Kapital, keine Dienstleistung für Dritte, keine marktmanipulativen Ordermuster (Art. 91 MiCA, §§ 46, 47 KMAG).
- **Wo Konto eröffnen?** Empfehlung: Bitvavo B.V. (MiCA-Lizenz der AFM, betreut deutsche Kunden direkt seit 01.09.2025, 0,15 % Maker / 0,25 % Taker, SEPA kostenlos, REST + WebSocket, ccxt-Unterstützung). Zweitkonto optional bei Kraken Pro als Fallback. Prüfenswert: OKX Europe (0,08 % / 0,10 %, MiCA-lizenziert), Bot-Tauglichkeit dort aber nicht recherchiert. Einschränkung: Freqtrade kann bei Bitvavo keinen Stop auf der Börse hinterlegen (Abschnitt 8.1, 10).
- **Kann Claude handeln?** Nein, nicht so, wie man es sich vorstellt. Ein Sprachmodell beobachtet den Markt nicht sekündlich, ist nicht deterministisch und hat in realen Tests Geld verloren (Alpha Arena 2025: 4 von 6 Modellen mit 31 bis 63 % Verlust in zwei Wochen). Claude hilft bei Strategie, Code, Backtest-Analyse und Review. Orders setzt ein deterministischer Bot mit harten Risikogrenzen.
- **Wie setzen wir es um?** Backtest (Abschnitt 6.5, 7) -> Dry-Run mindestens 3 Monate (Abschnitt 8, 11) -> optional Advisor im Schattenmodus (Abschnitt 5.3) -> Live mit 200 bis 500 EUR -> Skalieren nur nach Go-Kriterium (Abschnitt 11). Jede Stufe hat ein messbares Abbruchkriterium.
- **Was brauche ich?** Ein Bitvavo-Konto mit Trade-only-API-Key, einen unprivilegierten Debian-13-LXC (2 vCPU, 4 GB RAM), Freqtrade als Bot-Framework, Tailscale für den Zugriff, Telegram und einen Dead-Man's-Switch für Alarme, eine kleine USV für den Proxmox-Host, ein steuerfähiges Handelsprotokoll, einen lokalen vLLM-Server mit GPU für den optionalen Advisor (Schattenmodus zuerst) und Claude Code für Reviews. Kein Cloud-LLM im Live-Pfad.
- **Gebühren sind das zentrale Problem.** Ein Round-Trip kostet bei Bitvavo 0,30 % (Maker) bis 0,50 % (Taker). Die typische Kursbewegung einer 15-Minuten-Kerze liegt bei etwa 0,11 %, einer Stundenkerze bei etwa 0,28 %. Unterhalb von 4-Stunden- bis Tageskerzen ist kurzfristiger Handel mit Retail-Gebühren strukturell verlustbringend.
- **Ehrliche Erwartung:** Ein gut gebauter, gebührenbewusster, langsamer Long-only-Bot liefert eher "Buy-and-Hold-ähnliche Rendite mit geringerem Drawdown" als eine Outperformance. Die Wahrscheinlichkeit, Buy-and-Hold über mehrere Jahre zu schlagen, liegt unter 50 %. Kapitalmaximierung durch häufiges Handeln ist nicht belegt.
- **Steuern:** Jeder Verkauf innerhalb eines Jahres ist ein privates Veräußerungsgeschäft (§ 23 EStG), besteuert zum persönlichen Satz, Freigrenze 1.000 EUR pro Jahr (kein Freibetrag). Für das Kalenderjahr 2026 melden alle EU-Börsen erstmals deine aggregierten Umsätze und Transaktionszahlen an das BZSt (KStTG / DAC8). Buy-and-Hold hat durch die Einjahresfrist einen großen Nach-Steuer-Vorteil.
- **Die GmbH raushalten.** Privat handeln, mit privatem Kapital, auf einem eigenen Börsenkonto. Die GmbH-Variante kostet rund 30 % auf jeden Gewinn, ohne Freigrenze, mit voller Buchführung pro Fill.
- **Vorgehen:** Erst Backtest, dann mindestens 3 Monate Paper-Trading (Freqtrade Dry-Run, weil keine Börse eine brauchbare Spot-Sandbox hat), dann 200 bis 500 EUR live, dann skalieren. Jede Phase hat ein Go/No-Go-Kriterium (Abschnitt 11).
- **Wenn du das Projekt trotzdem machst:** als Lern- und Risikomanagement-System mit Kapital, das du verlieren kannst, gemessen gegen Buy-and-Hold und wöchentliches DCA.

### 1.1 Begriffe

| Begriff | Bedeutung in diesem Dokument |
|---|---|
| Round-Trip (RT) | Ein Kauf plus der zugehörige Verkauf. RT-Kosten = Kaufgebühr + Verkaufsgebühr + Spread + Slippage |
| Maker / Taker | Maker: deine Limit-Order liegt im Orderbuch und wird von jemand anderem ausgeführt (günstigere Gebühr). Taker: du führst sofort gegen eine liegende Order aus (teurer) |
| Post-only | Limit-Order, die die Börse ablehnt, falls sie sofort ausführbar wäre. Garantiert Maker-Gebühr, aber keinen Fill |
| Spread | Abstand zwischen bestem Kauf- und Verkaufskurs im Orderbuch, in Basispunkten (1 bps = 0,01 %) |
| Slippage | Differenz zwischen erwartetem und tatsächlichem Ausführungskurs |
| Hysterese-Band | Puffer um ein Signalniveau (z. B. 200-Tage-SMA), damit kleine Schwankungen um die Linie nicht ständig Ein- und Ausstiege auslösen |
| Vol-Targeting | Positionsgröße so wählen, dass die erwartete Schwankung des Depots einem Zielwert entspricht: Exposure = Zielvolatilität / gemessene Volatilität, gedeckelt bei 100 % |
| Rebalance-Band | Erst umschichten, wenn die Ist-Position vom Soll um mehr als einen Schwellenwert abweicht |
| Walk-forward | Backtest in rollierenden Fenstern: auf Fenster A optimieren, auf dem folgenden Fenster B testen, dann weiterschieben |
| Out-of-Sample (OOS) | Datenzeitraum, der bei der Optimierung nicht angefasst wurde und nur einmal zur Prüfung dient |
| Adverse Selection | Eine liegende Limit-Order füllt bevorzugt dann, wenn der Kurs gerade gegen dich läuft |
| STOR | Suspicious Transaction and Order Report: Verdachtsmeldung der Börse an die Aufsicht (Art. 92 MiCA) |
| Dead-Man's-Switch | Mechanismus, der auslöst, wenn ein erwartetes Lebenszeichen ausbleibt. Zwei Varianten hier: Alarm (Healthchecks) und börsenseitiges Auto-Cancel (Bitvavo `cancelOrdersAfter`) |
| Partial Fill | Eine Order wird nur teilweise ausgeführt; jeder Teil ist ein eigener Fill mit eigener Trade-ID |
| Dry-Run | Freqtrade-Modus mit echten Marktdaten, aber simulierten Orders und simulierter Wallet |

## 2. Rechtslage (Deutschland/EU)

### 2.1 Keine Erlaubnispflicht für Eigengeschäft

MiCA (Verordnung (EU) 2023/1114) gilt seit 30.12.2024. Das deutsche Ausführungsgesetz ist das KMAG. Eine Kryptowerte-Dienstleistung setzt Verträge "mit Kunden" voraus (Art. 3 Abs. 1 Nr. 15, 19, 20 MiCA). BaFin schreibt im Merkblatt "Kryptowerte-Dienstleistungen nach MiCAR" (03.01.2025, Abschnitt II.3): "Das Eigengeschäft mit Kryptowerten nach der MiCAR ist allerdings nicht zulassungspflichtig, da ein Vertragsabschluss mit (und für) Kunden erforderlich ist." Das gilt auch für automatisierte oder KI-gestützte Ausführung, solange nur dein Geld und dein Konto betroffen sind.

Die frühere KWG-Falle (§ 32 Abs. 1a KWG, Eigengeschäft als Mitglied eines MTF) greift bei Bitcoin-Spot nicht mehr: Das FinmadiG (BGBl. 2024 I Nr. 438, in Kraft 30.12.2024) hat Kryptowerte aus dem Katalog der Finanzinstrumente gestrichen (§ 1 Abs. 11 Satz 1 Nr. 10 KWG "weggefallen"). Achtung: BTC-Derivate (Futures, Perpetuals, CFDs) bleiben Finanzinstrumente. Für dieses Projekt: nur Spot, keine Derivate.

### 2.2 Wann eine Lizenz nötig wäre

Sobald du gewerbsmäßig (auf Dauer angelegt, mit Gewinnerzielungsabsicht) Dienstleistungen für Dritte erbringst:

| Tätigkeit | MiCA-Dienstleistung | Folge |
|---|---|---|
| Geld von Freunden/Familie mitverwalten | Portfolioverwaltung (Art. 3 Abs. 1 Nr. 25) | Erlaubnispflicht Art. 59 MiCA |
| Personalisierte Signale verkaufen | Beratung zu Kryptowerten (Nr. 24) | Erlaubnispflicht |
| Orders für andere ausführen | Ausführung von Aufträgen (Nr. 21) | Erlaubnispflicht |
| Bot als Service anbieten | je nach Ausgestaltung | Erlaubnispflicht |

Erbringen ohne Erlaubnis ist strafbar: § 46 Abs. 1 Nr. 3 KMAG, bis zu 5 Jahre Freiheitsstrafe, Versuch strafbar, fahrlässig bis zu 3 Jahre. Eine natürliche Person kann gar keine CASP-Lizenz bekommen. Es gibt keine Bagatell- oder Familienausnahme. Konsequenz: niemand "hängt sich an den Bot", keine Signale, keine Sammelkonten.

### 2.3 Marktmissbrauch gilt auch für Privatpersonen

MiCA Titel VI (Art. 86 bis 92) gilt für "jede Person", für alle Kryptowerte, die zum Handel zugelassen sind (BTC ist es), auf und außerhalb von Handelsplattformen, in der EU und in Drittstaaten. Art. 91 Abs. 1: "No person shall engage in or attempt to engage in market manipulation."

Bot-Verhaltensmuster, die als Manipulation gewertet werden können:

| Muster | Norm | Gegenmaßnahme |
|---|---|---|
| Eigene Orders matchen sich gegenseitig (Wash Trade) | Art. 91 Abs. 2 lit. a | Self-Trade-Prevention der Börse nutzen (Bitvavo `selfTradePrevention`, Kraken `stptype`); nur eine Strategie pro Konto |
| Schnelle Place/Cancel-Schleifen (Spoofing, Layering) | Art. 91 Abs. 3 lit. b | Wenige, langlebige Limit-Orders; kein Cancel/Replace pro Poll |
| Order-Sturm durch Bug | Art. 91 Abs. 3 lit. b (i) kann greifen, wenn der Orderfluss eine Wirkung nach Abs. 2 lit. a hat und keine "legitimen Gründe" vorliegen. Ein reiner Bug ist nicht automatisch Manipulation, löst aber die STOR-Pflicht der Börse (Art. 92) und AGB-Sanktionen aus (Coinbase Trading Rules 2.12: Stornierung bei "unreasonably burdens the platform"; Bitpanda API Terms 6.2 a: "runaway processes ... at your sole risk") | Globaler Order-Rate-Limiter, Max-Open-Orders, Circuit Breaker |
| Kursmeinung posten und von Kursreaktion profitieren, ohne Position offenzulegen | Art. 91 Abs. 3 lit. c | Nicht über Positionen posten, solange der Bot sie hält |

Sanktionen in Deutschland: Ordnungswidrigkeit nach § 47 Abs. 3 Nr. 113 KMAG mit Geldbuße bis 5 Mio. EUR für natürliche Personen; Straftat nach § 46 Abs. 2 KMAG (bis 5 Jahre), wenn der Kurs tatsächlich beeinflusst wird. Die Börse ist nach Art. 92 verpflichtet, verdächtige Ordermuster zu melden (STOR). Ihre Überwachung erkennt Muster, nicht Absichten. Deshalb: pro Order eine lesbare Begründung im Log speichern, um "legitime Gründe" belegen zu können. Dieses Log (`signal_reason` in `fills`, `decisions.jsonl`) wird so lange aufbewahrt wie das Steuerprotokoll (Abschnitt 3.5).

### 2.4 Was nicht gilt

- MiFID-II-Algo-Regeln (Art. 17, RTS 6: Meldung, Kill-Switch, Tests) adressieren Wertpapierfirmen und Handelsplatzmitglieder. Ein Privatkunde auf einer Krypto-Börse ist keins von beidem, BTC ist kein MiFID-Finanzinstrument.
- EU AI Act: Art. 2 Abs. 10 nimmt natürliche Personen bei rein persönlicher, nicht beruflicher Nutzung aus. Trading steht nicht in Anhang III (Hochrisiko). Würde die GmbH den Bot betreiben, wäre sie "Deployer" mit leichten Pflichten (Art. 4 KI-Kompetenz).

### 2.5 Börsen-AGB zu Bots

| Börse | Vertragslage |
|---|---|
| Bitvavo | Trading Rules definieren "API Trading Bot" ausdrücklich; jede Order braucht eine `operatorId`; Wash Trading verboten. Seiten selbst Cloudflare-geschützt, Text aus Suchauszügen (nicht abschließend verifiziert) |
| Kraken (PESL, EEA Terms 29.06.2026) | Support-Seite: Bots willkommen. Ziff. 11.5 verbietet aber pauschal "bots ... or any other forms of automation" auf "Our Content", Ziff. 16.1 verbietet Latenz-Arbitrage und "Scalping" veralteter Kurse; solche Trades können storniert werden. Vor Live-Betrieb schriftliche Bestätigung beim Support einholen |
| Bitpanda Fusion (API Terms v1.1.0, 14.07.2026) | Erlaubt Bots und KI-Agenten (auch Claude via MCP), 1.000 req/min, Keys laufen nach 1 Jahr ab, Marktmanipulation und Nutzung für Dritte verboten; jede authentifizierte Anfrage gilt als deine Weisung |
| Coinbase Luxembourg | Trading Rules verbieten Wash Trading, Spoofing, Layering, Quote Stuffing; Orders, die die Plattform belasten, können storniert werden |
| Trade Republic | Kundenvereinbarung (Stand 7/2026, III Ziff. 1.3) verbietet jede Nutzung über nicht bereitgestellte Zugangswege; Verstoß ist Kündigungsgrund. Unbrauchbar |
| BISON, Scalable Capital | Keine öffentliche API; Scalable bietet nur Krypto-ETPs. Unbrauchbar |
| Binance | Keine MiCA-Lizenz, Antrag in Griechenland am 24.06.2026 zurückgezogen, EU-Dienste ab 01.07.2026 abgebaut. Berichte vom Aug./Sept. 2026, dass weiterhin EU-Konten eröffnet werden (Reverse Solicitation). Für dieses Projekt nicht vertretbar |

### 2.6 Geldwäsche und Meldepflichten

- Häufiges Handeln erzeugt für dich keine GwG-Pflichten. Die Börse ist GwG-verpflichtet, überwacht Transaktionen und kann Herkunftsnachweise (Source of Funds / Wealth) verlangen. Halte Kontoauszüge und Gehaltsnachweise bereit; Transfers > 1.000 EUR auf Self-Custody-Wallets lösen Zusatzprüfungen aus.
- Das Kryptowerte-Steuertransparenzgesetz (KStTG, DAC8) ist seit 24.12.2025 in Kraft (Gesetz vom 22.12.2025, BGBl. 2025 I Nr. 352). Erster Meldezeitraum ist das Kalenderjahr 2026. Jede EU-Börse meldet pro Nutzer und Kryptowert die aggregierten Brutto-Käufe, Brutto-Verkäufe, Stückzahlen und die Anzahl der Transaktionen an das BZSt, erstmals bis 31.07.2027 für 2026. Schon der empfohlene langsame Bot (rund 50 Trades im Jahr, Abschnitt 3.2) meldet bei 5.000 EUR Kapital etwa 250.000 EUR Bruttoumsatz; ein hochfrequenter Bot mit 500 Trades käme auf rund 2,5 Mio. EUR. Beides fällt gegenüber einem kleinen Gewinn auf. Die Steuererklärung muss dazu passen.

## 3. Steuern

### 3.1 Grundregeln für Privatpersonen

Maßgeblich ist das BMF-Schreiben vom 06.03.2025 (IV C 1 - S 2256/00042/064/043), das den Brief vom 10.05.2022 ersetzt. Ein neueres Schreiben wurde nicht gefunden.

| Regel | Inhalt | Fundstelle |
|---|---|---|
| Einkunftsart | Private Veräußerungsgeschäfte, § 22 Nr. 2 i.V.m. § 23 Abs. 1 S. 1 Nr. 2 EStG | BMF Rn. 53 |
| Steuersatz | Persönlicher progressiver Tarif (2026: 14 % ab 12.349 EUR, 42 % ab 69.879 EUR, 45 % ab 277.826 EUR), keine Abgeltungsteuer | § 32a EStG |
| Haltefrist | Verkauf nach mehr als einem Jahr steuerfrei; die Frist ist ein Kalenderdatum (Anschaffungsdatum plus ein Jahr, § 108 AO i.V.m. §§ 187 ff. BGB), nicht 365 Tage | § 23 Abs. 1 EStG |
| Freigrenze | Gesamtgewinn aller § 23-Geschäfte im Jahr unter 1.000 EUR: steuerfrei; ab 1.000 EUR: alles steuerpflichtig | § 23 Abs. 3 S. 5 EStG |
| Jeder Trade | BTC gegen EUR, gegen andere Kryptowerte (auch Stablecoins) oder Waren ist eine Veräußerung; Frist beginnt für das erhaltene Asset neu | BMF Rn. 54, 55 |
| Zeitpunkt | Die von der Börse aufgezeichneten Zeitstempel sind maßgeblich, und zwar pro Fill | BMF Rn. 55 |
| Verbrauchsfolge | Einzelbetrachtung, sonst FIFO für die Haltefrist; walletbezogen; Methode je Wallet beibehalten bis zum vollständigen Verkauf | BMF Rn. 61, 62 |
| Gebühren | Verkaufsgebühren sind Werbungskosten, Kaufgebühren Anschaffungsnebenkosten | BMF Rn. 57, 59 |
| Verluste | Nur gegen § 23-Gewinne; Rücktrag nur ein Jahr, Vortrag unbegrenzt; Rücktrag auf Antrag komplett verzichtbar | § 23 Abs. 3 S. 7, 8 EStG |
| Solidaritätszuschlag | Nur wenn die festgesetzte Einkommensteuer 20.350 EUR (Einzel) übersteigt | § 3 Abs. 3 SolzG |

Drei Konsequenzen für das Design:

1. **BTC/EUR handeln, nicht BTC/USDC.** Ein Stablecoin-Round-Trip erzeugt zwei steuerpflichtige Veräußerungen mit eigener FIFO-Lot-Führung und EUR-Bewertung. BTC/EUR erzeugt eine, und der Fill-Preis ist der Erlös. Die günstigeren USDC-Gebühren bei Bitvavo sind in Abschnitt 4.2 abgewogen.
2. **Eigenes Börsenkonto nur für den Bot.** Liegen dort auch alte BTC (> 1 Jahr), gelten sie per FIFO als zuerst verkauft; die Rückkäufe des Bots starten die Uhr neu, und die Langfristposition verliert ihren Status. HODL-Bestände in eine separate Wallet oder ein separates Konto. Das gilt auch für DCA-Käufe: Sie laufen deshalb nicht im Bot-Konto (Abschnitt 6.4).
3. **Gebührenwährung prüfen.** Keine Quelle belegt, dass du bei Bitvavo die Gebührenwährung wählen kannst; der Support-Artikel zu Handelsgebühren sagt dazu nichts. Jeder Fill liefert über `GET /v2/trades` das Feld `feeCurrency`. Wird die Gebühr in BTC belastet, ist jede Gebühr eine Mini-Veräußerung und muss im Ledger als solche gebucht werden (Abschnitt 7.4).

### 3.2 Rechenbeispiel

Zwei Fälle. Der erste ist bewusst extrem und zeigt nur das Gebühren- und DAC8-Problem. Der zweite entspricht der empfohlenen Strategie (wenige Trades pro Monat, Abschnitt 6.4).

| Größe | Fall A: 500 Trades/Jahr (Illustration) | Fall B: ca. 50 Trades/Jahr (empfohlen) |
|---|---|---|
| Kapital | 5.000 EUR | 5.000 EUR |
| Bruttovolumen (Trades x Kapital) | ca. 2,5 Mio. EUR | ca. 250.000 EUR |
| Börsengebühren bei 0,15 bis 0,25 % je Seite | 3.750 bis 6.250 EUR | 375 bis 625 EUR |
| Bruttogewinn, der für 2.000 EUR netto nötig wäre | 5.750 bis 8.250 EUR (115 bis 165 % des Kapitals) | 2.375 bis 2.625 EUR (48 bis 53 % des Kapitals) |
| Bewertung | unrealistisch; Gebühren übersteigen das Kapital | ambitioniert, aber rechnerisch möglich |

Fall A wird in DAC8 mit 2,5 Mio. EUR Bruttoumsatz gemeldet. Deshalb muss der Bot Gebühren vor jeder Entscheidung einpreisen (Abschnitt 6).

Steuerrechnung für Fall B, Annahme: Nettogewinn nach Börsengebühren 2.000 EUR, alle Positionen unter einem Jahr.

| Position | Betrag |
|---|---|
| Nettogewinn nach Börsengebühren | 2.000 EUR |
| abzüglich weitere Werbungskosten (Serveranteil, Steuertool ~99 bis 129 EUR, API-Kosten); Werbungskosten-Ansatz mit Steuerberater klären, Quellenlage nur sekundär | ca. 200 EUR |
| Gesamtgewinn § 23 | 1.800 EUR |
| Freigrenze 1.000 EUR überschritten | gesamter Betrag steuerpflichtig |
| Einkommensteuer bei ~38,6 % Grenzsatz (zu versteuerndes Einkommen 60.000 EUR) | ca. 695 EUR |
| Einkommensteuer bei 42 % Grenzsatz (ab 69.879 EUR) | ca. 756 EUR |
| zzgl. Kirchensteuer 8 bis 9 % der Steuer, falls Mitglied | ca. 56 bis 68 EUR |
| Bei Gesamtgewinn 999 EUR | 0 EUR Steuer |
| Bei Gesamtgewinn genau 1.000 EUR | 386 bis 420 EUR Steuer (Klippeneffekt) |

### 3.3 Risiko "gewerblicher Handel"

BMF Rn. 52: Wiederholtes Kaufen und Verkaufen "kann ... eine gewerbliche Tätigkeit darstellen"; Abgrenzung nach den Kriterien des gewerblichen Wertpapierhandels (H 15.7 (9) EStH). Nach BFH X R 1/97 (20.12.2000) und X R 7/99 (30.07.2003) sind Anzahl und Umfang der Trades "nicht entscheidend". Entscheidend ist händlertypisches Verhalten: Handel für fremde Rechnung, Angebot an die Öffentlichkeit, direkte Geschäfte mit institutionellen Kontrahenten statt über eine Plattform, Ausnutzung von Erfahrungen aus einem einschlägigen Hauptberuf. Fremdfinanzierung in nennenswertem Umfang schadet nicht mehr.

Wichtige Einschränkung aus X R 7/99: Ein "Daytrader, der ganztägig mit spezieller EDV-Ausstattung am Echtzeithandel teilnimmt" kann dem Bild eines Finanzunternehmens entsprechen, wer dagegen "neben einer Hauptbeschäftigung ... in der Freizeit" handelt, nicht. Ein 24/7-Bot auf eigenem Server ist ein schwaches Indiz, das durch Hauptberuf, ausschließlich eigenes Kapital und Handel nur über die Börse entkräftet wird. Eine Gerichtsentscheidung zu Krypto-Bots gibt es nicht. Halte das Setup erkennbar privat: kein Fremdgeld, keine Signale, keine Werbung, keine GmbH-Infrastruktur.

Folgen bei Gewerblichkeit: § 15 EStG zum gleichen Tarif, Verlust der Einjahresfrist und der Freigrenze, Gewerbesteuer erst ab 24.500 EUR Gewerbeertrag (weitgehend anrechenbar nach § 35 EStG), Buchführung/GoBD, dafür volle Verlustverrechnung. Bei 2.000 EUR Gewinn ist die Steuerdifferenz klein, der Compliance-Aufwand nicht.

### 3.4 Variante GmbH (nicht empfohlen)

| Kriterium | Privat | GmbH |
|---|---|---|
| Steuer auf Gewinn | 0 bis 45 % (progressiv), 0 % nach 1 Jahr Haltefrist | ca. 30 % (15,825 % KSt + Soli, ca. 14 % GewSt bei Hebesatz 400 %), ohne Haltefrist |
| Freigrenze | 1.000 EUR | keine |
| Ausschüttung | entfällt | zusätzlich 26,375 % Abgeltungsteuer |
| Verluste | nur gegen § 23-Gewinne | gegen alle GmbH-Gewinne |
| Buchführung | Aufzeichnungen nach BMF Rn. 102 ff. | HGB-Doppik pro Fill, GoBD, Verfahrensdokumentation |
| Erlaubnis | keine (Eigengeschäft) | keine für Spot; bei Derivaten § 32 Abs. 1a KWG prüfen |
| AI Act | nicht anwendbar | Deployer-Pflichten (Art. 4) |
| KSt-Ausblick | entfällt | Senkung um 1 Prozentpunkt/Jahr ab 2028 auf 10 % bis 2032 |

Umsatzsteuer: BTC/EUR-Tausch ist steuerfrei (§ 4 Nr. 8 b UStG). Private Mittel nicht mit der operativen GmbH vermischen (verdeckte Einlage/Ausschüttung). Falls jemals GmbH, dann eine separate Trading-GmbH und nur bei Gewinnen weit oberhalb des Beispiels.

### 3.5 Aufzeichnungspflichten (BMF Rn. 87 bis 105)

- Beweislast liegt bei dir. Bei ausländischen Börsen (Bitvavo, Kraken, Coinbase, Bitpanda) gilt die erweiterte Mitwirkungspflicht nach § 90 Abs. 2 AO ("die Beteiligten [haben] diesen Sachverhalt aufzuklären und die erforderlichen Beweismittel zu beschaffen"), inklusive "regelmäßigem und vollständigem Abruf der Transaktionsübersichten" (Rn. 89). Datenverlust durch Börseninsolvenz oder Hack geht zu deinen Lasten.
- Pro Veräußerung müssen erkennbar sein: Kürzel, Menge, Anschaffungszeitpunkt und -kosten inkl. Gebühren in EUR, Plattform, Veräußerungszeitpunkt, Erlös und Kosten in EUR, Haltedauer, gewählte Verbrauchsfolge je Wallet, Transfers zwischen Wallets (Rn. 102, 103). Auf Verlangen: Bestände zum 31.12., Wallet-Adressen, Tx-Hashes (Rn. 104). Bei Teilausführungen ist jeder Fill ein eigener Vorgang mit eigenem Zeitstempel (Rn. 55).
- Steuerreports von Tools werden anerkannt, wenn plausibel und mit Report-Einstellungen (Kurse, FIFO) belegt (Rn. 90). Kann das Finanzamt die Grundlagen nicht ermitteln oder verletzt du die Mitwirkungspflicht nach § 90 Abs. 2 AO, wird geschätzt (§ 162 Abs. 1, Abs. 2 S. 1 AO).
- Formale 6-Jahres-Aufbewahrung nur ab 500.000 EUR Überschusseinkünften (Rn. 105), aber Festsetzungsverjährung 4 Jahre, bei Hinterziehung 10. Alles 10 Jahre aufbewahren. Das gilt für `fills`, `lots`, `disposals`, die monatlichen Börsen-CSVs und `decisions.jsonl` (Beleg für "legitime Gründe", Abschnitt 2.3). Nur das Betriebslog `freqtrade.log` und das Journal dürfen rotieren (Abschnitt 8, Schritt 13).
- Anlage SO: Die Quellen widersprechen sich, ob das Formular ab Steuerjahr 2025 einen eigenen Abschnitt "Kryptowerte" hat oder Kryptowerte weiter unter "andere Wirtschaftsgüter" laufen; ein amtliches Formular wurde nicht geprüft (nicht abschließend verifiziert). Auch Verlustjahre und Jahre unter der Freigrenze erklären, damit Verluste festgestellt werden und die DAC8-Meldung nicht unerklärt bleibt.

### 3.6 Steuertools

| Tool | Preis (Stand 2026) | Anmerkung |
|---|---|---|
| Blockpit | 49 EUR (bis 50 Tx), 99 EUR (bis 1.000), 149 EUR (bis 3.000), 229 EUR (bis 10.000) je Steuerjahr | DACH-fokussiert, FIFO, Anlage-SO-Ausgabe; Kraken-Importmodus nicht verifiziert |
| CoinTracking | Pro ca. 129 bis 169 USD/Jahr (3.500 Tx kumuliert über alle Jahre) | München; Kraken via OAuth read-only; Preise auf zwei Seiten abweichend, vor Kauf prüfen |
| Koinly | nicht abrufbar (403) | Englisch, weniger DACH-spezifisch |
| Accointing | eingestellt 31.01.2024 | nicht einplanen |

Da der Bot ohnehin jeden Fill protokolliert, kann er den FIFO-Report im Format von Rn. 102/103 selbst erzeugen; ein Tool dient dann als unabhängige Gegenprobe.

### 3.7 Beobachten: Referentenentwurf September 2026

Presseberichte vom 7. bis 11.09.2026 (btc-echo, extraetf, krypto-besteuern.de, Haufe) beschreiben einen BMF-Referentenentwurf: Gewinne aus Kryptowerten, die nach dem 31.12.2026 angeschafft werden, sollen als Kapitaleinkünfte mit 25 % Abgeltungsteuer ohne Haltefrist besteuert werden, mit Quellensteuerabzug durch Anbieter ab 2028 und Verlustverrechnung mit Wertpapieren. Bestandsschutz für Altbestände. Der Entwurf ist nicht im Kabinett und nicht im Bundestag; der Text war nicht abrufbar (nicht abschließend verifiziert). Für einen Intra-Jahres-Bot wäre 25 % günstiger als 38 bis 42 %, aber die steuerfreie Einjahresfrist entfiele. Das Handelsprotokoll muss Lots vor und nach dem 31.12.2026 trennen können. Vor Jahresende 2026 erneut prüfen.

## 4. Börse und Konto

### 4.1 Vergleich

Alle Angaben Stand 15.09.2026. MiCA-Übergangsfristen sind abgelaufen (Deutschland 31.12.2025, EU-weit 01.07.2026). Nur Anbieter im ESMA-Register (Stand 09.09.2026, 343 Einheiten) dürfen deutsche Kunden bedienen.

| Börse / Einheit | MiCA-Status | Maker / Taker Basisstufe | Round-Trip Maker / Taker | SEPA | API | ccxt | Freqtrade | Bewertung |
|---|---|---|---|---|---|---|---|---|
| Bitvavo B.V. (NL, AFM 26.06.2025) | ja, DE seit 01.09.2025 direkt | 0,15 % / 0,25 % (bis 100k EUR/30 Tage) | 0,30 % / 0,50 % | frei ein und aus | REST + WS, 1.000 Punkte/min, Keys View/Trade/Withdraw, IP-Whitelist, `cancelOrdersAfter` (Dead-Man's-Switch) | ja + ccxt.pro | community-tested, `operatorId` nötig, kein `stoploss_on_exchange` | Empfehlung |
| Kraken Pro (Payward Europe Solutions Ltd, IE, CBI 25.06.2025) | ja, DE seit 01.08.2025 | 0,40 % / 0,80 % (Tier 1 seit 09.07.2026); Tier 3 0,22 % / 0,38 % ab 10k USD Volumen oder 20k USD Guthaben | 0,80 % / 1,60 % | frei (DE-IBAN) | REST + WS v2, Key-Ablaufdatum, IP-Restriktion, Cancel-Strafpunkte, `CancelAllOrdersAfter` | ja + ccxt.pro | offiziell, `stoploss_on_exchange` | Fallback; 2,7 bis 3,2x teurer als Bitvavo |
| OKX Europe Ltd (MT, MFSA 27.01.2025) | ja, DE passportiert | 0,08 % / 0,10 % (Regular User, EUR-Seite) | 0,16 % / 0,20 % | nicht recherchiert | nicht recherchiert | ja | offiziell als `myokx`, `stoploss_on_exchange` | Günstigster lizenzierter Anbieter; Bot-AGB, Key-Scopes, Spread (1,2 bis 2,7 bps gemessen) nicht abschließend verifiziert |
| Bitstamp Europe S.A. (LU, CSSF 15.05.2025) | ja | 0,30 % / 0,40 % | 0,60 % / 0,80 % | Einzahlung frei, Auszahlung 3 EUR | REST + WS, echte Sandbox (Zugang unklar), Min-Order 10 EUR | ja | nein | solider Dritter |
| Bitpanda Fusion (Bitpanda GmbH, AT) | ja (BaFin/FMA/Malta) | 0,25 % / 0,25 % (Level 1) + ca. 0,05 % Spread | ca. 0,60 % | frei | nur REST (WS "in Entwicklung"), 1.000 req/min, Keys 1 Jahr, MCP-Server für Claude | nein | nein | Watchlist; Prinzipal-Modell, kein Orderbuch |
| Coinbase Advanced (Coinbase Luxembourg S.A., CSSF 20.06.2025) | ja | Sekundärquellen widersprechen sich: 0,60 % / 1,20 % (Intro 1) bzw. 0,40 % / 0,60 % (Basisstufe); Primärseite nicht abrufbar (nicht abschließend verifiziert) | 1,20 % / 2,40 % bzw. 0,80 % / 1,20 % | frei | REST + WS, Sandbox nur statische Mock-Daten | ja | nein (Datenlücken bekannt) | in jeder Lesart teurer als Bitvavo |
| One Trading Exchange B.V. (NL) | ja | 0,10 % / 0,20 % | 0,20 % / 0,40 % | - | BTC_EUR Spot am 15.09.2026 im Status CLOSED (Live-Abfrage) | ja | nein | derzeit unbrauchbar |
| Binance | nein | 0,10 % / 0,10 % | 0,20 % | - | - | ja | offiziell | ausgeschlossen |
| BISON, Trade Republic, Scalable | TR: Art. 60 (ESMA-Register); BISON/Scalable: nicht recherchiert | Spread 1,25 % bzw. 1 EUR + Spread bzw. ETPs | - | - | keine API / vertraglich verboten | nein | nein | ausgeschlossen |

Mindestordergrößen BTC/EUR (Live-Abfrage 15.09.2026): Bitvavo 5 EUR, Kraken 0,00005 BTC / 0,45 EUR, Bitstamp 10 EUR, Coinbase 1 EUR. Bitvavo-Spread BTC/EUR gemessen: Median 0,6 bps; Kraken 2,7 bis 8 bps (Methode: Abschnitt 6.1).

Keine Börse bietet eine brauchbare Spot-Sandbox. Paper-Trading läuft über Freqtrade Dry-Run gegen Live-Marktdaten.

### 4.2 Empfehlung

1. **Bitvavo als Hauptbörse.** Günstig, EUR-nativ, engster Spread, WebSocket, ccxt, Bots vertraglich vorgesehen, MiCA-Lizenz, direkter Vertrag mit deutschen Kunden. Kostenlose Ein- und Auszahlungen (Support-Artikel 4405188690065, nur als Suchauszug lesbar). Limit-Orders reservieren 0,25 % und erstatten die Differenz bei Maker-Ausführung (Support-Artikel 4405175148689). Der "engste Spread" stützt sich auf zwei Quellen: Kaikos Report "The State of the European Crypto Market 2025-2026" (Mai 2026; 0,981 bps im Mittel über BTC-, ETH-, SOL-, XRP- und ADA-EUR-Paare; der Report wurde laut eigener Angabe von Bitvavo bezahlt) und die eigene Messung vom 15.09.2026 (Median 0,6 bps BTC/EUR). Nachteil: Freqtrade kann bei Bitvavo keinen Stop auf der Börse hinterlegen (Abschnitt 8.1).
2. **BTC/USDC bei Bitvavo: geprüft und verworfen.** Bitvavo rechnet BTC-USDC in Gebührenkategorie B mit 0,05 % / 0,05 % ab, also 0,10 % Round-Trip statt 0,30 % bis 0,50 % bei BTC-EUR (Kategorie A). Trotzdem nein: Jeder Round-Trip erzeugt zwei steuerpflichtige Veräußerungen (BTC und USDC, BMF Rn. 54), USDC braucht eigene FIFO-Lots und eine EUR-Bewertung je Fill, und du trägst EUR/USD-Risiko. Die Tagesrate-Toleranz in Rn. 91 macht die Bewertung handhabbar, verdoppelt aber die Aufzeichnungen. Bei wenigen Trades im Jahr (Abschnitt 6.4) ist die Gebührenersparnis kleiner als der Mehraufwand und das Fehlerrisiko.
3. **OKX Europe prüfen**, bevor du dich festlegst: Die Gebühren liegen bei etwa der Hälfte, Freqtrade unterstützt die EWR-Einheit offiziell (`myokx`) inklusive `stoploss_on_exchange`. Nicht recherchiert wurden Bot-Klauseln, Key-Berechtigungen, SEPA-Konditionen und ob die Gebührentabelle für deutsche Konten gilt. Erst nach eigener Prüfung nutzen.
4. **Kraken Pro optional als Zweitkonto**, weil die API-Dokumentation exzellent ist, `stoploss_on_exchange` und ein Dead-Man's-Switch dokumentiert sind und ccxt den Wechsel zur Konfigurationsfrage macht. Bei unter 20k USD Guthaben aber deutlich teurer.
5. **Nicht:** Binance, BISON, Trade Republic, Scalable, One Trading (BTC/EUR geschlossen).

### 4.3 Kontoeröffnung Bitvavo, Schritt für Schritt

| Schritt | Tätigkeit | Hinweis |
|---|---|---|
| 1 | Registrieren mit E-Mail und starkem Passwort, 2FA (TOTP) sofort aktivieren | Privatkonto, nicht GmbH |
| 2 | Identität in der App verifizieren (Personalausweis oder Reisepass + Selfie) | 5 bis 15 Minuten, bis 24 h |
| 3 | Erste SEPA-Einzahlung von einem Bankkonto auf deinen Namen | Wird Referenzkonto für Auszahlungen; Mindesteinzahlung nicht abschließend verifiziert, Mindestorder 5 EUR |
| 4 | In der App die aktuelle Gebührentabelle (Kategorie A, Stufe 0: 0,15 / 0,25 %) und die Trading Rules zu Bots lesen | Seiten waren automatisiert nicht abrufbar |
| 5 | Settings -> API: drei Keys nach der Tabelle unten anlegen | **Niemals Withdraw.** Bitvavo-Doku: Auszahlungen per API umgehen 2FA |
| 6 | IP-Whitelist setzen, wenn deine öffentliche IP statisch ist | Sonst siehe 4.5 |
| 7 | Nur das Arbeitskapital auf der Börse lassen, Gewinne periodisch per SEPA abziehen | Auszahlungslimit 25.000 EUR/24 h (Support-Artikel, Suchauszug, nicht abschließend verifiziert) |

Key-Scopes:

| Key | Berechtigung | Verwendung |
|---|---|---|
| `bot-dryrun` | View | Freqtrade Dry-Run (liest nur Marktdaten und Bilanz, sendet keine Orders) |
| `bot-live` | View + Trade | Freqtrade Live, erst ab Phase 4 anlegen |
| `readonly` | View | Dashboard-Ergänzung, `ledger.py`, Steuertool |

Alle Keys ohne Withdraw, mit IP-Whitelist wo möglich, und mit Rotation. Freqtrade liest die Keys aus `secrets.env`; Dry-Run und Live nutzen getrennte Env-Dateien.

### 4.4 Kontoeröffnung Kraken Pro (falls Zweitkonto)

Konto anlegen, 2FA, Verifizierungsstufe "Intermediate" (Ausweis, Adressnachweis, Beruf) für SEPA, Einzahlung über die deutsche Banking-Circle-IBAN. Dann Kraken Pro -> Settings -> API: Key mit **Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades, Create & Modify Orders, Cancel/Close Orders**, IP-Restriktion, Ablaufdatum (z. B. 90 Tage) mit Rotation. Kein Withdraw Funds. Bei Kraken Cancel/Replace von Orders unter 5 Sekunden vermeiden (Strafpunkte im Rate-Counter).

### 4.5 IP-Whitelist und Heimanschluss

Die Whitelist setzt eine stabile öffentliche IP voraus. Deutsche Privatanschlüsse haben oft dynamische IPv4 oder DS-Lite/CGNAT (allgemeines Wissen, nicht für deinen Anschluss geprüft). Optionen: statische IP beim Provider, Egress des Containers über einen kleinen VPS mit fester IP per WireGuard, oder Verzicht auf die Whitelist mit Kompensation durch Trade-only-Scope und kurze Key-Laufzeit.

## 5. Wie "Claude handelt": Rolle der KI und Guardrails

### 5.1 Warum kein LLM den Markt beobachtet

- **Latenz und Kosten:** Ein Claude-Aufruf mit Denken dauert Sekunden bis Dutzende Sekunden. Ein 4.000-Token-Kontext plus 300-Token-Antwort kostet auf Sonnet 5 rund 0,011 USD, auf Opus 5 rund 0,0275 USD. Alle 5 Minuten wären das rund 95 USD/Monat (Sonnet 5, ohne Caching), stündlich rund 8 USD, alle 4 Stunden rund 2 USD.
- **Nicht-Determinismus:** Anthropic dokumentiert, dass auch bei Temperatur 0 identische Eingaben unterschiedliche Ausgaben liefern können. Eine LLM-Entscheidung ist deshalb nicht reproduzierbar backtestbar.
- **Empirie:** Alpha Arena (Nof1, echtes Geld, 10.000 USD je Modell, Hyperliquid, 17.10. bis 03.11.2025): GPT-5 -62,7 %, Gemini 2.5 Pro -56,7 %, Grok 4 -45,3 %, Claude Sonnet 4.5 -30,8 %, DeepSeek V3.1 +4,9 %, Qwen3 Max +22,3 %. Trefferquoten 25 bis 30 %. Gebühren fraßen 13 bis 17 % des Startkapitals in 2,5 Wochen (Zahlen: Protos, 04.11.2025). Der Gründer Jay Azhang in seinem X-Post vom 03.11.2025: "LLMs don't really handle numerical time series data very well." Season 1.5 (US-Aktien): nur 6 von 32 Modell-Modus-Kombinationen profitabel. TradeRank (simuliert, Season 8, Sept. 2026): 0 von 14 Modellen schlug Bitcoin Buy-and-Hold. Akademische Studien: Framework und Risikoregeln erklären mehr als das Modell; Vorhersageaufgaben zeigen "near-total failure". Backtests innerhalb des Trainingsfensters sind durch Look-ahead kontaminiert.
- **Verantwortung:** Jede authentifizierte API-Anfrage gilt als deine Weisung (Bitpanda API Terms 3.4, 6.2 g). Die Börse unterscheidet nicht, ob ein Modell die Order ausgelöst hat.

### 5.2 Rollenverteilung

| Rolle | Wer | Wann |
|---|---|---|
| Marktdaten abrufen, Signale berechnen, Orders senden, Stops, Positionsgröße, Kill-Switch | Deterministischer Bot (Freqtrade) | jede Iteration (ca. 5 s), Entscheidungen auf geschlossenen 4h/1d-Kerzen |
| Strategie entwerfen und als Code umsetzen | Claude (Claude Code auf dem Git-Repo) | offline, vor Backtest |
| Backtests, Lookahead-Analyse, Hyperopt-Ergebnisse interpretieren, Overfitting erkennen | Claude | offline, nach jedem Lauf |
| Code-Review, Log-Analyse, Anomalie-Erklärung, Wochenbericht | Claude | wöchentlich, per Claude Code oder Routine auf Git-Repo |
| Optional: Regime-Einschätzung als strukturiertes JSON | Advisor-Service mit lokalem vLLM-Modell (`btctrader-advisor`) | stündlich, nur als Gate, erst nach Schattenmodus |
| Nie | Claude | Orders platzieren, Stops setzen, Exits übersteuern |

Kerzenschluss und Zeitzone: Freqtrade rechnet Kerzen in UTC. Eine Tageskerze schließt um 00:00 UTC (02:00 MESZ, 01:00 MEZ), eine 4h-Kerze um 00:00, 04:00, 08:00 UTC usw. Alle Entscheidungen, Logs und Zeitstempel im Ledger werden in UTC geführt; das hält Backtest, Dry-Run und Live vergleichbar und passt zu den Börsen-Zeitstempeln nach BMF Rn. 55.

### 5.3 Optionaler Advisor: Design

Entscheidung vom 15.09.2026: Der Advisor läuft lokal gegen einen vLLM-Server (OpenAI-kompatible API) auf einem eigenen GPU-Host im Heimnetz, nicht gegen eine Cloud-API. Ein systemd-Timer ruft stündlich `btctrader-advisor run` auf. Das Skript baut einen kompakten Marktkontext nur aus öffentlichen Bitvavo-Daten (letzte 30 Tageskerzen, 24 4h-Kerzen, SMA50/200-Abstand, realisierte Volatilität, Returns, Drawdown; optional Bot-Status), schickt ihn an das lokale Modell und verlangt eine schemavalidierte Antwort:

```json
{"regime": "risk_on | neutral | risk_off", "confidence": 0.0, "rationale": "..."}
```

Regeln:
- Structured Outputs erzwingen das Schema, aber nicht den Inhalt; Wertebereiche in Code prüfen (Schema unterstützt kein minimum/maximum).
- Jede Antwort mit Prompt-Hash, Modell, Antwort, Token-Verbrauch in `decisions.jsonl` loggen.
- Ergebnis atomar nach `decision.json` schreiben; die Freqtrade-Strategie liest die Datei in `bot_loop_start` (Mikrosekunden) und nutzt sie nur als Entry-Gate oder Positionsgrößen-Modifikator.
- Kein Netzwerkaufruf in `confirm_trade_entry` / `confirm_trade_exit`; nie einen Stoploss-Exit ablehnen (Freqtrade-Doku: "can cause significant losses").
- Stabiler System-Prompt zuerst, volatile Daten danach (vLLM Prefix-Caching); `temperature 0`, fester `seed`; vLLM Structured Outputs (`response_format` mit JSON-Schema) erzwingen das Format.
- Modellwahl nach VRAM des GPU-Hosts, siehe `docs/VLLM_ADVISOR.md`. Der Advisor hat keine Börsen-Keys und ruft nie Order-Endpunkte auf.
- Erst mindestens 6 Monate Schattenmodus (Entscheidungen geloggt, nicht konsumiert) auf Daten nach dem Trainings-Cutoff, dann Vergleich gated vs. ungated im Dry-Run.

Voraussetzungen: ein GPU-Host im Heimnetz mit vLLM (Docker-Compose in `deploy/vllm/`), vom Container über das LAN erreichbar; laufende Kosten sind Strom statt API-Gebühren. Claude Code wird nur für Reviews des Repos genutzt; ob dafür Routines oder manuelle Sitzungen zum Einsatz kommen, ist eine reine Komfortfrage (Abschnitt 12).

Claude Code Routines eignen sich nicht als Live-Schleife: Sie laufen in der Anthropic-Cloud, mindestens stündlich, ohne Zugriff auf dein LAN. Sie eignen sich für den wöchentlichen Review: Routine zieht Backtest-Ergebnisse und Decision-Logs aus dem Git-Repo, prüft den Strategiecode und öffnet einen PR mit Vorschlägen, den du freigibst.

### 5.4 Guardrails in Code, nicht in Prompts

| Guardrail | Umsetzung | Freqtrade nativ / eigener Code |
|---|---|---|
| Harter Stoploss | Pflichtfeld `stoploss` in der Strategie; bei Bitvavo nur bot-seitig (Abschnitt 8.1) | nativ |
| Positionsgröße | `max_open_trades = 1`, fester `stake_amount`, kein Margin, keine Perps | nativ |
| Order-Timeout | `unfilledtimeout` bricht unausgeführte Orders ab; Teilausführung bleibt als Position, wenn sie über der Mindestordergröße liegt | nativ |
| Serien-Stops, Abkühlung, Drawdown-Pause | Protections `StoplossGuard` (zählt Stoploss-Exits im Lookback), `CooldownPeriod` (Pair-Sperre nach Exit), `MaxDrawdown` (Handelspause nach Drawdown der geschlossenen Trades) | nativ; alle drei sperren nur neue Einstiege, keine stellt etwas glatt |
| Tagesverlustlimit in % des Equity | `StoplossGuard` zählt Stops, nicht Equity; eigener Check in `bot_loop_start`: Equity heute vs. Tagesstart, bei -3 % keine neuen Entries und Telegram-Alarm | eigener Code |
| Max-Drawdown-Kill-Switch, der glattstellt | 20 % vom Equity-Hoch: `forceexit` aller Positionen über die REST-API, Bot in `stopentry`, manueller Reset. Freqtrades `MaxDrawdown` pausiert nur Entries | eigener Code (Service neben dem Bot) |
| Abgleich | Börsen-Bilanz vs. Freqtrade-DB und `fills` jede Iteration; Stopp bei Abweichung | eigener Code |
| Order-Rate-Limiter | Max. Orders pro Minute/Tag, Max-Open-Orders; Freqtrade begrenzt nur über `max_open_trades` | eigener Code (Wrapper in `confirm_trade_entry`, ohne Netzwerkaufruf) |
| Idempotenz | Freqtrade verfolgt Orders über die Börsen-Order-ID und gleicht offene Orders beim Start aus seiner DB ab (`startup_update_open_orders`). Bitvavo unterstützt `clientOrderId` ("Must be unique across open orders"); ob ccxt/Freqtrade sie bei Bitvavo setzen, ist nicht abschließend verifiziert. Ledger gleicht über `exchange_trade_id` ab | nativ (Order-ID) + eigener Code (Ledger) |
| Self-Trade-Prevention | Bitvavo Default `decrementAndCancel`; in `ccxt_config.options` explizit setzen | nativ über ccxt-Optionen |
| Watchdog | systemd `WatchdogSec` startet einen hängenden Prozess neu; Healthchecks alarmiert. Beide können bei Ausfall nichts auf der Börse canceln (Abschnitt 8.1) | nativ (`--sd-notify`) + Timer |
| Börsenseitiger Dead-Man's-Switch | Bitvavo `POST /v2/cancelOrdersAfter` (10 bis 300 s, `codGroupId`), Kraken `CancelAllOrdersAfter`; ccxt `cancelAllOrdersAfter`. Freqtrade ruft das nicht auf; ein eigener Timer muss den Countdown alle 60 s erneuern | eigener Code, optional (Abschnitt 8.1) |
| Parameteränderungen | nur nach manueller Freigabe (Git-PR) | Prozess |
| API-Key | Trade-only, kein Withdraw, IP-Whitelist wo möglich, Rotation | Börse |

## 6. Strategie und Gebühren

### 6.1 Break-even-Rechnung

Ein Round-Trip muss mindestens Kaufgebühr + Verkaufsgebühr + Spread + Slippage verdienen.

| Anbieter / Stufe | Maker RT | Taker RT | Spread BTC/EUR (gemessen) |
|---|---|---|---|
| Bitvavo Stufe 0 | 0,30 % | 0,50 % | ca. 0,6 bis 1,4 bps |
| OKX Europe Regular | 0,16 % | 0,20 % | ca. 1,2 bis 2,7 bps (nicht abschließend verifiziert) |
| Bitstamp Basis | 0,60 % | 0,80 % | - |
| Kraken Tier 2 (ab 2,5k USD/30 Tage) | 0,60 % | 1,20 % | ca. 2,7 bis 8 bps |
| Kraken Tier 1 | 0,80 % | 1,60 % | ca. 2,7 bis 8 bps |
| Coinbase Advanced Intro (unverifiziert) | 1,20 % / 0,80 % | 2,40 % / 1,20 % | - |

Typische Bewegung von BTC/EUR im September 2026 (realisierte Volatilität ca. 40 bis 47 % p.a., Deribit DVOL 37 bis 41):

| Kerze | 1-Sigma-Bewegung (Normalnäherung bei 50 % p.a.) | mittlere absolute Bewegung (gemessen, Kraken XBT/EUR) |
|---|---|---|
| 1 Minute | ca. 0,07 % | - |
| 15 Minuten | ca. 0,18 % | 0,11 % |
| 1 Stunde | ca. 0,45 % | 0,28 % |
| 4 Stunden | ca. 0,9 % | - |
| 1 Tag | ca. 2,5 % | 1,8 % |

Lies die Tabelle so: Die linke Spalte ist eine Obergrenze aus der Normalverteilungs-Näherung (Sigma_Tag = Vol/sqrt(365), skaliert mit sqrt(Kerzen pro Tag)). Die rechte Spalte ist die gemessene mittlere Bewegung und für den Gebührenvergleich maßgeblich. Die mittlere absolute Bewegung liegt bei etwa 0,8 Sigma.

Quellen und Methode der Messungen (alle 15.09.2026): Deribit DVOL aus der öffentlichen API `get_volatility_index_data` (Tageswerte 08. bis 15.09.2026). Realisierte Volatilität und mittlere Bewegung aus Kraken-OHLC (öffentliche API): 30 Tageskerzen, 720 Stundenkerzen, 720 15-Minuten-Kerzen XBT/EUR. Spread: 20 Orderbuch-Abfragen über etwa eine Minute bei Bitvavo (`/v2/ticker/book`), Kraken, Coinbase und OKX, jeweils bei BTC um 65.200 EUR. Das sind Stichproben eines Tages, keine Langzeitstatistik.

Auf 1-Minuten- bis Stundenkerzen liegt die typische Bewegung auf oder unter den günstigsten Maker-Kosten von 0,30 %. Erst bei 4-Stunden- bis Tageskerzen übersteigt die Bewegung die Kosten um ein Mehrfaches.

Erforderliche Trefferquote p bei Ziel T, Stop S und Round-Trip-Gebühr f: p > (S + f) / (T + S).

| Ziel T | Stop S | Gebühr f | nötige Trefferquote |
|---|---|---|---|
| 0,5 % | 0,5 % | 0,5 % | 100 % (unmöglich) |
| 0,5 % | 0,5 % | 0,3 % | 80 % |
| 3,0 % | 1,5 % | 0,5 % | 44 % |
| 3,0 % | 1,5 % | 0,2 % | 38 % |

Jährlicher Gebührenabfluss = Round-Trips pro Jahr x RT-Kosten auf das gehandelte Volumen:

| Frequenz | RT-Kosten 0,5 % | RT-Kosten 0,3 % |
|---|---|---|
| 1 Round-Trip pro Tag | ca. 182 % des Kapitals p.a. | ca. 110 % |
| 1 Round-Trip pro Woche | 26 % | 16 % |
| 10 Round-Trips pro Jahr | 5 % | 3 % |

### 6.2 Gebührenbewusste Regeln für den Bot

1. **Kostenregel.** Jeder Kandidat muss `expected_move > k * (fee_entry + fee_exit + spread + slippage)` mit k >= 3 erfüllen. Ein Trendfilter liefert keinen "erwarteten Move". Für die Startstrategie (Abschnitt 6.4) wird die Regel so umgesetzt: (a) Hysterese-Band um den 200-Tage-SMA von mindestens 3x RT-Kosten, also bei 0,30 % RT mindestens 1 %, empfohlen 2 bis 3 %; (b) Mindesthaltedauer nach jedem Einstieg (z. B. 5 Tageskerzen) gegen Whipsaw; (c) Rebalance-Band in EUR und in Prozentpunkten (Punkt 3); (d) vor jeder Order prüft `confirm_trade_entry` bzw. `adjust_trade_position`, dass die Positionsänderung in EUR mindestens 3x die RT-Kosten dieser Änderung verdient, sonst keine Order.
2. **Börsenparameter zur Laufzeit lesen, nicht hart codieren.** Gebühren: Bitvavo `GET /v2/account` (liefert `fees.maker`, `fees.taker`, `fees.volume`), Kraken `TradeVolume`. Marktregeln aus Bitvavo `GET /v2/markets?market=BTC-EUR` (Live-Abfrage 15.09.2026): `quantityDecimals` 8, `notionalDecimals` 2, `minOrderInBaseAsset` 0,00007324 BTC (dynamisch, ändert sich mit dem Kurs), `minOrderInQuoteAsset` 5 EUR, `tickSize` 1 EUR, `maxOpenOrders` 400. ccxt und Freqtrade runden Mengen und Preise mit `amount_to_precision` / `price_to_precision` auf diese Werte; Freqtrade reserviert zusätzlich `amount_reserve_percent` (5 %) plus Stoploss beim Mindest-Stake. Folge: Eine Rebalance unter 5 EUR wird abgelehnt. Das Rebalance-Band muss deshalb in EUR bemessen sein, nicht nur in Prozentpunkten (Punkt 3).
3. **Rebalance-Band.** Umschichten nur, wenn die Abweichung vom Soll-Exposure größer ist als 15 bis 20 Prozentpunkte **und** der Orderwert mindestens max(5 EUR, 3x RT-Kosten der Order in EUR, 2 % des Kapitals) beträgt. Bei 500 EUR Kapital sind das mindestens 10 EUR, bei 5.000 EUR mindestens 100 EUR.
4. **Post-only-Limit-Orders als Standard (Maker).** Taker nur, wenn der erwartete Move auch die Taker-Kosten deckt. Fill-Rate und realisierte Slippage loggen, weil Maker-Orders bevorzugt füllen, wenn der Kurs gegen dich läuft (Adverse Selection).
5. **Teilausführungen (Partial Fills).** Eine Limit-Order kann in mehreren Fills ausgeführt werden. Jeder Fill hat bei Bitvavo eine eigene Trade-ID, einen eigenen Zeitstempel, eigene Gebühr und `feeCurrency` (`GET /v2/trades`). Freqtrade führt pro Trade mehrere Order-Objekte und aggregiert die Fills; nach `unfilledtimeout` storniert es den Rest und behält eine Teilposition, wenn ihr Wert über der Mindestordergröße liegt (sonst wird die Stornierung verweigert, weil die Position nicht mehr verkaufbar wäre). Im Ledger ist jeder Fill eine eigene Zeile und jede Teilveräußerung ein eigener § 23-Vorgang (Abschnitt 7.4).
6. **Kein Cancel/Replace pro Poll-Zyklus.**
7. **Backtest realistisch.** Gebühren, Spread und Slippage im Backtest ansetzen (eigene Stufe + 5 bps Slippage). Freqtrade füllt im Backtest ohne Slippage, sobald der Preis in der Kerze liegt, und wendet `fee` zweimal an.

### 6.3 Was realistisch ist

Empirie zum aktiven Handel ist einheitlich negativ: 97 % der brasilianischen Daytrader mit mehr als 300 Handelstagen verloren Geld, ohne Lerneffekt (Chague et al.); die handelsaktivsten US-Haushalte erzielten 11,4 % statt 17,9 % (Barber/Odean, Ursache fast vollständig Transaktionskosten); BIS schätzt, dass 73 bis 81 % der Retail-Bitcoin-App-Nutzer 2015 bis 2022 verloren haben. Backtest-Overfitting ist bei Parametersuche fast automatisch: Mit 5 Jahren Daten garantieren mehr als etwa 45 getestete Konfigurationen einen In-Sample-Sharpe von 1 bei erwartetem Out-of-Sample-Sharpe von 0 (Bailey et al.).

| Strategieklasse | Gebührentoleranz | Belegter Nutzen |
|---|---|---|
| Wöchentliches DCA | sehr hoch (nur Kaufgebühr) | Benchmark; Lump Sum schlägt DCA in 58 bis 72 % der Fälle (Analystenquelle, geringe Belastbarkeit) |
| Trendfolge auf Tageskerzen (200-Tage-SMA, Donchian 20/55) | hoch (wenige Trades pro Jahr) | Drawdown-Reduktion in Bärenphasen 2018/2022; Outperformance gegen B&H nicht belegt |
| Volatilitäts-Targeting mit Rebalance-Band | hoch | Man Group: +0,4 Sharpe bei 30 % Zielvolatilität (institutionell, ohne Retail-Gebühren) |
| Trendfolge auf 1h-Kerzen | niedrig | arXiv 2602.11708: im Portfolio-Backtest über 150+ Paare 847 Trades/Monat auf H1 gegenüber 41 auf D1, Kostenmodell 4 bps je Trade; die Autoren: "H1 and H4 suffer from excessive turnover and transaction costs". Auf ein einzelnes Paar heruntergebrochen sind das grob 5 bis 6 Trades/Monat, aber bei 15 bis 25 bps Retail-Gebühren statt 4 bps schrumpft der dort gemessene Vorteil von H1 gegenüber D1 deutlich oder kippt |
| Grid-Trading | niedrig | Erwartungswert "essentially zero", Inventarrisiko im Trend; Grid-Schritt muss >= 3x RT-Kosten sein |
| Mean Reversion 1h, Scalping 1m bis 15m, LLM pro Kerze | keine | negativer Erwartungswert bei Retail-Gebühren |

Realistisches Ziel: B&H-ähnliche Rendite mit geringerem Drawdown, plus ein Lernsystem. Nach Steuern hat B&H durch die Einjahresfrist einen zusätzlichen Vorteil, den der Dashboard-Vergleich abbilden sollte.

### 6.4 Empfohlene Startstrategie (Spot, Long-only, kein Hebel)

1. **Passives Bein als virtuelle Benchmark.** Wöchentliches DCA in BTC wird nicht im Bot-Konto ausgeführt, sondern in `ledger.py` rechnerisch geführt (jede Woche zum Kerzenschluss ein fiktiver Kauf zum Marktkurs mit Taker-Gebühr). Grund: Echte DCA-Käufe im Bot-Konto würden per walletbezogener FIFO (BMF Rn. 61/62) durch die Verkäufe des Bots aufgezehrt und verlören ihren Einjahresstatus (Abschnitt 3.1, Konsequenz 2). Wer tatsächlich DCA halten will, macht das in einem separaten Konto oder einer separaten Wallet und außerhalb dieses Projekts.
2. **Trendfilter auf Tageskerzen (UTC-Schluss):** long, wenn Schlusskurs über 200-Tage-SMA (oder Donchian-Ensemble 20/55 Tage), sonst flat; Hysterese-Band 2 bis 3 % gegen Whipsaw, Mindesthaltedauer 5 Tageskerzen.
3. **Volatilitäts-Targeting:** Exposure = min(1, Zielvol 30 bis 40 % / realisierte Vol), Rebalance nur bei Drift > 15 bis 20 Prozentpunkte und Orderwert über dem EUR-Band aus Abschnitt 6.2 Punkt 3.
4. Erwarteter Umsatz: wenige Trades pro Monat, in der Größenordnung von 30 bis 60 Fills im Jahr. Polling alle 1 bis 5 Minuten nur für Monitoring, Risikochecks und Order-Management.
5. Vergleich ab Tag 1 gegen reines B&H und die virtuelle DCA-Benchmark.

### 6.5 Validierungsleiter

| Stufe | Kriterium |
|---|---|
| Backtest 2017 bis 2026 mit eigener Gebührenstufe + 5 bps Slippage | muss 2018 und 2022 enthalten; `lookahead-analysis` und `recursive-analysis` bestanden |
| Walk-forward mit gesperrtem Out-of-Sample-Jahr | jede getestete Konfiguration protokolliert, < 45 pro 5 Jahre Daten |
| Dry-Run auf Live-Daten | >= 3 Monate, Backtest-vs-Dry-Run-Drift wöchentlich geprüft |
| Live mit 5 bis 10 % des Zielkapitals | 3 bis 6 Monate |
| Skalieren | nur wenn Netto-Rendite pro Drawdown-Einheit B&H im gleichen Fenster schlägt; < 100 Trades gelten als statistisch nicht aussagekräftig |

Datenquelle für den Backtest: Bitvavo existiert erst seit 2018 und liefert keine Historie bis 2017. Die BTC/EUR-Kerzen kommen deshalb aus Krakens Quartals-CSVs der Trades (XBTEUR), die in `user_data/data/kraken/trades_csv/` liegen und mit `freqtrade convert-trade-data --exchange kraken --format-from kraken_csv --format-to feather` und `freqtrade trades-to-ohlcv -p BTC/EUR --exchange kraken -t 1h 4h 1d` in Kerzen umgewandelt werden (RAM-hungrig, laut Freqtrade-Doku mehr als bei jeder anderen Börse). Der Backtest läuft dann mit `--datadir /srv/trading/user_data/data/kraken` und `--fee 0.0015` (Bitvavo Maker) bzw. `0.0025` (Taker). Das ist eine Näherung: Kraken-Kurse mit Bitvavo-Gebühren, ohne Bitvavo-Spread und -Liquidität. Für Tageskerzen ist der Unterschied klein; für den Dry-Run zählen dann die echten Bitvavo-Daten.

## 7. Architektur

### 7.1 Build vs. Adopt

| Option | Lizenz / Stand | Bewertung |
|---|---|---|
| **Freqtrade** 2026.8 (31.08.2026), 54,4k Stars, GPL-3.0, Python >= 3.11 | monatliche Releases | **Empfehlung.** Dry-Run mit simulierter Wallet, Backtesting inkl. Lookahead/Recursive-Analyse und P-Wert, Hyperopt, FreqUI, Telegram, REST-API + WebSocket, SQLite, Gebühren aus ccxt, Protections, Kraken offiziell, Bitvavo community-tested, OKX-EWR als `myokx` |
| Eigenbau FastAPI + ccxt | - | Nur sinnvoll als Dashboard-Schicht über der Freqtrade-API. Order-State-Tracking, Gebührenbuchhaltung, Dry-Run und Persistenz müssten nachgebaut werden |
| Hummingbot 2.16.0 | Apache-2.0 | Market-Making-Framework, falsche Form für Single-Pair-Spot |
| Jesse 3.1.5 | MIT-Kern | Live/Paper nur mit lizenziertem Plugin, braucht PostgreSQL + Redis |
| OctoBot | GPL-3.0, 3.0 Beta | Major-Version-Umbruch, Cloud-Bindung |
| Gekko | archiviert 2020 | nicht verwenden |
| FreqAI (LightGBM/XGBoost/PyTorch in Freqtrade) | - | Deterministische, backtestbare ML-Alternative zum LLM-Advisor, null Kosten pro Aufruf; Beispielstrategie nicht produktionsreif |

Freqtrade-Hinweise: Der Konfigurationswert `fee` wird nur in Dry-Run/Backtest beachtet; live gelten die echten Fills. Kraken liefert nur 720 historische Kerzen, daher `download-data --dl-trades` (langsam, RAM-hungrig) oder Krakens Quartals-CSVs über `convert-trade-data --format-from kraken_csv` (Abschnitt 6.5). Für Bitvavo `ccxt_config.options.operatorId` (Integer) setzen; aktuelles ccxt verlangt sie bei jeder Order. Kraken: `ccxt_async_config.rateLimit: 3100`. `stoploss_on_exchange`: Freqtrade-Tabelle "Exchange features" (Stand 15.09.2026) listet Kraken (Spot: market, limit) und OKX (Spot: market, limit), Bitvavo steht mit "not available".

### 7.2 Komponenten

```
+---------------------------------------------------------------------+
| Proxmox VE Host (ZFS oder LVM-thin, chrony, vzdump, Firewall, USV)  |
|                                                                     |
|  +---------------------------------------------------------------+  |
|  | LXC "btc-bot" (unprivilegiert, Debian 13, /dev/net/tun)       |  |
|  |                                                               |  |
|  |  freqtrade.service (venv, sd_notify, Restart=always)          |  |
|  |    strategie BtcTrend.py  <-- liest decision.json (Gate)      |  |
|  |    ccxt --> Bitvavo REST/WS (Trade-only Key, kein Withdraw)   |  |
|  |    SQLite tradesv3.sqlite (Trades, Orders)                    |  |
|  |    Telegram-Bot (/status /profit /stopentry)                  |  |
|  |    REST-API + FreqUI auf 127.0.0.1:8080                       |  |
|  |                                                               |  |
|  |  btctrader-advisor.timer (stündlich, optional)                |  |
|  |    advisor --> vLLM (LAN, GPU-Host) --> decisions.jsonl, decision.json |  |
|  |                                                               |  |
|  |  ledger.timer (taeglich)                                      |  |
|  |    Fills von Boerse (GET /v2/trades) --> fills, lots,         |  |
|  |    disposals; Abgleich mit tradesv3.sqlite; Equity-Snapshot,  |  |
|  |    B&H- und DCA-Benchmark, CSV-Archiv                          |  |
|  |                                                               |  |
|  |  guard.timer (1 min) --> Equity-Check, Kill-Switch,           |  |
|  |    Bilanzabgleich, optional cancelOrdersAfter-Erneuerung      |  |
|  |  heartbeat.timer (1 min) --> Healthchecks / Uptime Kuma Push  |  |
|  |  tailscaled --> tailscale serve 8080 (HTTPS, nur Tailnet)     |  |
|  +---------------------------------------------------------------+  |
|                                                                     |
|  LXC/VM "monitoring": Uptime Kuma, ntfy (optional Grafana)          |
+---------------------------------------------------------------------+
        |                              |
   Tailscale (Handy, Laptop)      Git-Repo (Strategie, Config ohne Secrets)
                                       |
                                  Claude Code / Routine: Review, PRs
```

### 7.3 Verzeichnislayout im Container

```
/opt/freqtrade/                 Git-Checkout + .venv (setup.sh -i)
/srv/trading/user_data/
  config.json                   dry_run, pairlist BTC/EUR, api_server, telegram, protections
  config-private.json           0600: Keys, Telegram-Token, jwt_secret (nie ins Repo)
  strategies/BtcTrend.py        Trendfolge + Vol-Targeting
  strategies/BtcAdvisorGated.py Variante mit decision.json-Gate
  data/kraken/                  Feather-Kerzen 2017 bis heute aus Kraken-Trade-CSVs (Backtest)
  data/kraken/trades_csv/       Krakens Quartals-Zips (XBTEUR.csv), Rohdaten
  data/bitvavo/                 Feather-Kerzen ab Dry-Run-Start (Live-Daten)
  logs/freqtrade.log            RotatingFileHandler, 10 MB x 10 Dateien (Freqtrade-Default)
  backtest_results/, hyperopt_results/
  tradesv3.dryrun.sqlite        getrennte DB für Dry-Run
  tradesv3.sqlite               Live-DB
/srv/trading/advisor/
  advisor.py, prompts/system.md, decision.json, decisions.jsonl (10 Jahre aufbewahren)
/srv/trading/ledger/
  ledger.py, equity_daily.csv, fifo_lots.sqlite, exports/YYYY-MM-bitvavo-trades.csv (+ .sha256)
/srv/trading/guard/
  guard.py                      Equity-Check, Kill-Switch, Bilanzabgleich, cancelOrdersAfter
/etc/freqtrade/secrets.env      0640, FREQTRADE__EXCHANGE__KEY/SECRET (Trade-Key, erst Phase 4)
/etc/freqtrade/btctrader.env    0640, Konfiguration der eigenen Dienste inkl. ADVISOR_BASE_URL (vLLM), RO-Key (siehe docs/KOMPONENTEN.md)
/etc/freqtrade/secrets-dryrun.env  0600, View-only-Key für den Dry-Run
/etc/systemd/system/            freqtrade-dryrun.service, freqtrade.service, btctrader-advisor.{service,timer}, btctrader-ledger.{service,timer}, btctrader-guard.{service,timer}, btctrader-heartbeat.{service,timer}, btctrader-dashboard.service
```

### 7.4 Datenmodell des Handelsprotokolls

Quelle der Wahrheit für `fills` ist die Börse, nicht Freqtrade: `ledger.py` zieht die Fills über `GET /v2/trades` (Bitvavo; ccxt `fetchMyTrades`) und gleicht sie gegen `tradesv3.sqlite` ab. Grund: Das Finanzamt vergleicht mit den Aufzeichnungen der Handelsplattform (BMF Rn. 89) und mit der DAC8-Meldung der Börse (KStTG § 11). Freqtrades DB ist die Betriebssicht; bei Abweichungen gilt der Börsen-Datensatz, und die Abweichung wird als Vorfall geloggt. Der Abgleich läuft über `exchange_trade_id`, nicht über die Order, weil eine Order mehrere Fills haben kann.

Tabelle `fills` (append-only, jede Zeile mit Hash der Vorzeile; eine Zeile pro Fill, auch bei Teilausführungen):

| Feld | Typ | Zweck |
|---|---|---|
| id | integer | laufend |
| exchange, account_id | text | Plattform (BMF Rn. 103) |
| exchange_trade_id | text | Bitvavo `id` des Fills; Abgleichschlüssel |
| exchange_order_id, client_order_id | text | Zuordnung zur Order, Idempotenz |
| ts_exchange_utc | datetime | maßgeblicher Zeitstempel je Fill (BMF Rn. 55) |
| pair, side | text | BTC/EUR, buy/sell |
| amount_btc | decimal(18,8) | Menge (8 Nachkommastellen, Bitvavo `quantityDecimals`) |
| price_eur | decimal(18,2) | Fill-Preis = Erlös bzw. Anschaffungspreis |
| gross_eur | decimal | amount x price |
| fee_amount, fee_currency, fee_eur | decimal, text, decimal | Werbungskosten / Anschaffungsnebenkosten (Rn. 59); `fee_currency` aus dem Fill; ist sie BTC, erzeugt `ledger.py` zusätzlich eine Mini-Veräußerung in `disposals` |
| net_eur | decimal | netto |
| maker_taker | text | aus Bitvavo-Feld `taker`; Gebührenvalidierung |
| balance_btc_after, balance_eur_after | decimal | Abgleich mit Börse |
| strategy, signal_reason | text | "legitime Gründe" (Art. 91 Abs. 2 lit. a MiCA) |
| advisor_decision_id | text | Verknüpfung zum LLM-Log |
| dry_run | boolean | Trennung |
| reconciled_with_bot_db | boolean | Fill in `tradesv3.sqlite` gefunden |

Tabelle `lots` (FIFO je Konto/Wallet, BMF Rn. 61/62):

| Feld | Zweck |
|---|---|
| lot_id, buy_fill_id | Zuordnung (ein Lot pro Kauf-Fill) |
| acquired_at, qty_btc, cost_eur_incl_fees | Anschaffung |
| remaining_qty | Restmenge |
| pre_2027 | Flag für den Referentenentwurf |

Tabelle `disposals` (eine Zeile pro Verkaufs-Fill und verbrauchtem Lot):

| Feld | Zweck |
|---|---|
| sell_fill_id, lot_id, qty_btc | Zuordnung |
| acquisition_ts, disposal_ts, holding_days | Haltefrist, informativ |
| proceeds_eur, cost_eur, sell_fee_eur, gain_eur | § 23-Ergebnis |
| taxable | Steuerpflicht: `disposal_date <= acquired_date + 1 Jahr` als Kalenderdatum (§ 23 Abs. 1 Nr. 2 EStG "nicht mehr als ein Jahr", Fristberechnung nach § 108 AO i.V.m. §§ 187 ff. BGB). Nicht `holding_days <= 365`: In einem Schaltjahr sind 366 Tage noch "ein Jahr". Grenzfall (Verkauf genau am Jahrestag) mit dem Steuerberater klären |

Ergänzend: `transfers` (Tx-Hash, Gegen-Wallet, keine Veräußerung), `equity_daily` (Datum, EUR, BTC, Kurs, Equity, B&H-Benchmark, virtuelle DCA-Benchmark, kumulierte Gebühren), `decisions` (Prompt-Hash, Modell, Antwort, Tokens, Kosten), `expenses` (Werbungskosten mit Beleg). Monatlicher Export der Börsen-CSV mit SHA-256 ins Archiv und außer Haus (Rn. 89). Jahresausgabe: eine CSV pro Veräußerung nach Rn. 102 plus die Summen für Anlage SO.

## 8. Proxmox-LXC-Setup

Docker im LXC wird von Proxmox nicht unterstützt (Staff-Aussage 12.11.2025; runc/containerd-Regression 2025, erst mit PVE 9.1 behoben). Deshalb: Freqtrade nativ im unprivilegierten LXC ohne Docker. Wenn du Docker willst, dann in einer kleinen VM.

| Schritt | Umsetzung |
|---|---|
| 1 Container | Erst `pveversion` prüfen (Ziel PVE >= 9.1, Abschnitt 12). Dann auf dem Host: `pct create 210 local:vztmpl/<debian-13-standard-template>.tar.zst --hostname btc-bot --unprivileged 1 --cores 2 --memory 4096 --swap 512 --rootfs local-zfs:32 --net0 name=eth0,bridge=vmbr0,ip=dhcp --onboot 1 --startup order=20,up=30`. Keine `--features` (kein nesting/keyctl nötig). 8 GB RAM bei Hyperopt/FreqAI/Kraken-CSV-Konvertierung. rootfs auf ZFS oder LVM-thin (für vzdump-Snapshot-Modus). `--onboot 1` und `--startup order=` sorgen dafür, dass der Bot nach einem Host-Neustart ohne Handgriff wiederkommt; Monitoring-CT mit kleinerer `order` davor starten |
| 2 Zeit | Kein NTP-Client im Container, der Kernel-Clock des Hosts wird geteilt; chrony auf dem Host gesund halten. Wichtig für API-Nonces |
| 3 Tailscale | `pct set 210 --dev0 /dev/net/tun` (oder GUI Device Passthrough), im CT `tailscale up`. Voraussetzungen für `tailscale serve 8080`: in der Tailscale-Admin-Konsole MagicDNS aktivieren und unter "HTTPS Certificates" HTTPS einschalten. Ergebnis-URL: `https://btc-bot.<tailnet>.ts.net`. Achtung: Der Maschinenname landet im öffentlichen Certificate-Transparency-Log (Tailscale-Doku: "Do not enable the HTTPS feature if any of your machine names contain sensitive information"); also einen unverfänglichen Hostnamen wählen. Kein Funnel, kein Port-Forward am Router |
| 4 Freqtrade | Nutzer `freqtrade` anlegen (`useradd -r -m -d /srv/trading -s /usr/sbin/nologin freqtrade`), `git clone` nach `/opt/freqtrade` (Owner freqtrade), `./setup.sh -i` als dieser Nutzer (legt `/opt/freqtrade/.venv` an), dann `/opt/freqtrade/.venv/bin/freqtrade create-userdir --userdir /srv/trading/user_data` und `... new-config --config /srv/trading/user_data/config.json` |
| 5 systemd | Vollständige Unit siehe unten (System-Service, kein User-Service; Vorlage ist Freqtrades `freqtrade.service.watchdog`). sd_notify funktioniert nicht in Docker, hier schon |
| 6 Secrets | `FREQTRADE__EXCHANGE__KEY` / `__SECRET` nur in `/etc/freqtrade/secrets.env` (0640, root:freqtrade) oder `config-private.json`; die eigenen Dienste lesen `/etc/freqtrade/btctrader.env` (RO-Key, vLLM-Adresse, Telegram); nie im Repo; `secrets-dryrun.env` mit View-only-Key für den Dry-Run, `secrets.env` mit Trade-Key erst ab Phase 4 |
| 7 API/FreqUI | `listen_ip_address` 127.0.0.1 oder Tailscale-IP, `jwt_secret_key` >= 32 Zufallszeichen, starkes Passwort, `ws_token`. FreqUI hat kein HTTPS, daher nur über Tailscale |
| 8 Firewall | Default-Deny mit dokumentierter Allowlist. Eingehend: nur Tailnet (100.64.0.0/10) und LAN-Admin-Subnetz. Ausgehend erlaubt: DNS (53 UDP/TCP zum Resolver); 443 TCP zu `api.bitvavo.com`, `ws.bitvavo.com`, `api.telegram.org`, `hc-ping.com` (oder eigener Healthchecks-Host), `api.coingecko.com` (nur wenn `fiat_display_currency` gesetzt bleibt; Freqtrade nutzt CoinGecko für die EUR-Umrechnung); 80/443 zu `deb.debian.org`, `security.debian.org`, `pypi.org`, `files.pythonhosted.org`, `github.com` (Schritte 4 und 11); LAN: TCP 8000 zum vLLM-Host (Advisor); Tailscale: UDP 41641 ausgehend, UDP 3478 (STUN), TCP 443 zu `login.tailscale.com`, `controlplane.tailscale.com`, `log.tailscale.com` und den DERP-Relays `derp*.tailscale.com`. Die Proxmox-Firewall filtert nach IP; Bitvavo und andere stehen hinter Cloudflare mit wechselnden IPs. Praktikabel ist deshalb: ausgehend nur die genannten Ports freigeben und die Hostliste als Dokumentation führen, oder IP-Sets per Skript aus DNS aktualisieren |
| 9 Backup | Täglicher vzdump im Snapshot-Modus, `keep-daily 7, keep-weekly 4, keep-monthly 3`, auf PBS oder NAS; zusätzlich Off-Box-Kopie von `tradesv3.sqlite`, `fifo_lots.sqlite`, `decisions.jsonl`, Configs und CSV-Exporten. Bind-Mounts werden von vzdump nicht gesichert |
| 10 Monitoring | `heartbeat.timer` pingt jede Minute eine Healthchecks-URL nach erfolgreichem `GET /api/v1/health`; Uptime Kuma (separater CT/VM) pollt die API über das Tailnet; Alarme an Telegram + ntfy (self-hosted mit `auth-default-access: deny-all`; iOS-Sofortpush braucht `upstream-base-url`) |
| 11 Updates | Freqtrade-Version pinnen, monatliches Changelog lesen, erst im Dry-Run-Container aktualisieren |
| 12 Rate Limits | Bitvavo: weit unter 1.000 Gewichtspunkten/min bleiben, Preise über WebSocket (5.000 msg/s); bei HTTP 429 Backoff |
| 13 Log-Aufbewahrung | Betriebslogs rotieren, Nachweise nicht. `freqtrade.log`: Freqtrade-Default `RotatingFileHandler` mit 10 MB x 10 Backups (über `log_config` anpassbar). Journal: in `/etc/systemd/journald.conf` `SystemMaxUse=500M` und `MaxRetentionSec=1year`. Dauerhaft (10 Jahre, Abschnitt 3.5): `fills`, `lots`, `disposals`, `decisions.jsonl`, CSV-Exporte. `decisions.jsonl` niemals rotieren, sondern jährlich in eine Datei pro Jahr umbenennen und ins Off-Box-Backup nehmen |
| 14 USV | Kleine USV (Line-Interactive, USB) am Proxmox-Host. NUT auf dem Host: `upsmon` setzt bei "on battery + low battery" das FSD-Flag und ruft `SHUTDOWNCMD` (z. B. `/sbin/shutdown -h +0`); Proxmox fährt dann die Container geordnet herunter. Ziel: keine halb geschriebene SQLite-DB und kein abgeschnittener Fill. Nach Netzrückkehr startet der Host, `--onboot 1` startet den CT, `Restart=always` den Bot (Schritt 1, 5). Einmal testen: Stecker ziehen, Ablauf beobachten |

Vollständige Unit `/etc/systemd/system/freqtrade.service`:

```ini
[Unit]
Description=Freqtrade BTC/EUR bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=1min
StartLimitBurst=5

[Service]
Type=notify
NotifyAccess=all
User=freqtrade
Group=freqtrade
WorkingDirectory=/srv/trading
EnvironmentFile=/etc/freqtrade/secrets.env
ExecStart=/opt/freqtrade/.venv/bin/freqtrade trade \
  --userdir /srv/trading/user_data \
  --config /srv/trading/user_data/config.json \
  --config /srv/trading/user_data/config-private.json \
  --strategy BtcTrend \
  --logfile /srv/trading/user_data/logs/freqtrade.log \
  --sd-notify
Restart=always
RestartSec=10
TimeoutStartSec=1min
# mindestens 2 x internals.process_throttle_secs (Default 5 s)
WatchdogSec=20

[Install]
WantedBy=multi-user.target
```

Für den Dry-Run dieselbe Unit als `freqtrade-dryrun.service` mit `EnvironmentFile=/etc/freqtrade/secrets-dryrun.env`, eigener Config (`dry_run: true`, eigene `db_url`, eigener API-Port, eigener Telegram-Bot). Aktivieren mit `systemctl daemon-reload && systemctl enable --now freqtrade-dryrun.service`.

### 8.1 Was passiert, wenn der Container, das Internet oder die Börse ausfällt

| Szenario | Verhalten | Konsequenz und Maßnahme |
|---|---|---|
| Container oder Heimanschluss fällt aus, Bot hält eine Long-Position | Die Position liegt auf der Börse wie bei Buy-and-Hold. Bei Bitvavo gibt es keinen Stop auf der Börse (Freqtrade: `stoploss_on_exchange` "not available" für Bitvavo). Der bot-seitige Stoploss wird erst nach Neustart ausgewertet | Das ist das größte operative Restrisiko dieses Setups. Bei einer Tageskerzen-Strategie ist ein Ausfall von Stunden selten entscheidend, ein Ausfall von Tagen in einem Crash schon. Minderung: USV (Schritt 14), Autostart (Schritt 1, 5), Alarm binnen 3 Minuten (Schritt 10), Handy-App der Börse für einen manuellen Notverkauf. Wer einen Börsen-Stop will, braucht Kraken oder OKX (Abschnitt 12) |
| Container fällt aus, eine Post-only-Limit-Order liegt im Buch | Die Order bleibt bei Bitvavo aktiv und kann füllen, während der Bot down ist. Freqtrade speichert offene Orders in seiner DB und gleicht sie beim Start gegen die Börse ab (`startup_update_open_orders`: "Updates open orders based on order list kept in the database"). Ein zwischenzeitlicher Fill wird also nachgetragen | Akzeptabel für Entry-Orders, unangenehm für Exit-Orders. Optional: börsenseitiger Dead-Man's-Switch. Bitvavo `POST /v2/cancelOrdersAfter` mit `expiryAfterSeconds` 10 bis 300 und `codGroupId` (ccxt `cancelAllOrdersAfter`, Timeout 10.000 bis 300.000 ms); Kraken `CancelAllOrdersAfter` (Empfehlung Kraken: alle 15 bis 30 s mit Timeout 60 s aufrufen). Freqtrade ruft das nicht auf; `guard.timer` müsste den Countdown erneuern. Nachteil: Der Switch storniert alle offenen Orders der Gruppe, also auch eine liegende Exit-Order. Deshalb Default: aus, erst in Phase 4 mit Mini-Position testen |
| Börse liefert HTTP 5xx, 429 oder Timeouts | Freqtrade wiederholt Aufrufe (`API_RETRY_COUNT` 4, bei Orderabfragen 5, mit quadratischem Backoff) und bei `TemporaryError` wartet die Hauptschleife `RETRY_TIMEOUT` und versucht es erneut. Die Position bleibt. Ein Exit ist erst möglich, wenn die API wieder antwortet | Nichts zu tun außer Alarm: Uptime Kuma meldet, wenn `/health` keinen frischen `last_process` liefert. Kein manuelles Cancel/Replace während einer Störung |
| Bot wirft `OperationalException` (Config-Fehler, unerwarteter Zustand) | Freqtrade stoppt den Handel (`State.STOPPED`) und schickt eine Telegram-Nachricht mit Traceback und dem Hinweis, `/start` erst nach Prüfung zu senden | Offene Position bleibt unbewacht, bis du reagierst. Alarm über Telegram plus Healthchecks (der Prozess lebt, handelt aber nicht; deshalb pingt `heartbeat.timer` nur, wenn `/health` einen frischen Loop meldet) |
| Stromausfall am Host | Ohne USV: harter Stopp, Risiko für SQLite-Dateien und Journal. Mit USV: geordnetes Herunterfahren nach NUT-Regeln | USV (Schritt 14), vzdump-Backup, Off-Box-Kopie der DBs |
| `stoploss_on_exchange` im Dry-Run | Freqtrade erzeugt im Dry-Run nur simulierte Orders, auch für Stops (`create_stoploss` gibt bei `dry_run` eine Dry-Run-Order zurück). Ein Börsen-Stop wird im Dry-Run nie platziert | Falls die Börse Kraken oder OKX ist: `stoploss_on_exchange` aktivieren und in Phase 4 mit einer Mini-Position live prüfen, ob der Stop im Orderbuch der Börse erscheint |

## 9. Dashboard / Depot-Übersicht

### 9.1 Was die Übersicht zeigen soll

| Kachel / Chart | Quelle |
|---|---|
| Equity in EUR (Cash + BTC x Kurs), Kurve seit Start | Freqtrade Wallet-Balance-History (täglich seit 2026.4) oder `equity_daily` |
| Buy-and-Hold- und DCA-Benchmark, Überrendite | eigener `ledger.py` (Freqtrade zeigt "Market change" nur im Backtest) |
| Drawdown-Kurve, Max-Drawdown Bot vs. B&H | `equity_daily` |
| Realisierter / unrealisierter Gewinn, Sharpe, Sortino, Calmar, CAGR, Trefferquote | Freqtrade `/api/v1/profit` |
| Kumulierte Gebühren, Gebührenabfluss in % p.a., Maker-Fill-Rate, realisierte Slippage | `fills` |
| Offene Orders, letzte Trades, Trades pro Monat, Zeit im Markt | `/status`, `/trades` |
| Bot-Status: letzte Schleife, Kill-Switch-Zustand, Advisor-Regime | `/health`, `decision.json` |
| Steuer-Panel: YTD § 23-Gewinn nach Werbungskosten, YTD Gebühren, Abstand zur 1.000-EUR-Freigrenze, offene Lots mit Einjahresdatum | `disposals`, `lots` |

### 9.2 Einfache Variante (empfohlen zum Start)

FreqUI (mitgeliefert, aktuell 3.1.2 vom 30.08.2026, mobiles Layout gefixt) über Tailscale. Zeigt offene Positionen, Trade-Liste, Gewinnzusammenfassung, Tages/Wochen/Monats-PnL, Wallet-Balance-Verlauf inkl. unrealisiertem PnL, Kerzencharts mit Entries/Exits, Backtesting-Modus. `fiat_display_currency = EUR`. Telegram `/profit`, `/balance`, `/daily` gratis dazu.

Einschränkung beim Wallet-Balance-Verlauf: Daten vor der Markierung "Capture start" sind laut FreqUI-Doku nur "best-effort" nachgefüllt, enthalten keine Ein- und Auszahlungen und unterstellen als Startbilanz "current balance - profit/losses". Verlässlich ist die Kurve erst ab dem Start der Aufzeichnung. Freqtrade 2026.8 macht die Einbeziehung des unrealisierten PnL zudem börsenabhängig (`balance_includes_unrealized_pnl`). Für Benchmark und Steuer bleibt `equity_daily` aus `ledger.py` maßgeblich.

Ergänzung mit wenig Code: eine statische Seite (FastAPI + htmx `hx-trigger="every 15s"` + Chart.js mit `chartjs-adapter-date-fns`, oder Streamlit mit `st.fragment(run_every="30s")`), die Benchmark, Gebühren und Steuer-Panel aus SQLite rendert; wöchentlich ein QuantStats-HTML-Tearsheet (`qs.reports.html(returns, benchmark=btc_returns, periods_per_year=365)`).

### 9.3 Vollständige Variante (später)

PostgreSQL 16 bis 18 mit TimescaleDB 2.30 (nur PG 16 bis 18 unterstützt), idealerweise in einer VM, Grafana mit TimescaleDB-Toggle und Read-only-DB-Rolle, Alerting an Telegram. Community-Dashboards für Trading-Bots sind rar und veraltet (neuestes Freqtrade-Dashboard im Grafana-Katalog von 2024, Prometheus-basiert); Panels selbst bauen. Nicht InfluxDB 3 Core (Standard-Abfragefenster 72 Stunden, kein Compactor).

## 10. Risiken und Grenzen

| Risiko | Einschätzung | Minderung |
|---|---|---|
| Strategie hat keinen Edge nach Kosten | hoch; Standardfall bei Retail-Bots | langsame Kerzen, Maker-Orders, Benchmark gegen B&H, Abbruchkriterien |
| Backtest-Overfitting | hoch | Konfigurationszähler, Walk-forward, gesperrtes OOS-Jahr, Lookahead-Analyse |
| Bug erzeugt Order-Sturm oder Wash Trades | mittel | Rate-Limiter, STP, Max-Open-Orders, Circuit Breaker, Dry-Run zuerst |
| API-Key-Kompromittierung | mittel | Trade-only, kein Withdraw, IP-Whitelist, Rotation, 0600-Secrets, nur Arbeitskapital auf der Börse |
| Börse storniert Trades (Mistrade, "Scalping"-Klausel Kraken) | niedrig | keine Latenz-Arbitrage, schriftliche Bestätigung bei Kraken |
| Börsenausfall, Kontosperre (AML-Prüfung), Insolvenz | niedrig bis mittel | Herkunftsnachweise bereithalten, zweite Börse via ccxt, Gewinne abziehen, CSV-Archiv |
| Steuerliche Umqualifizierung als gewerblich | niedrig | privat, eigenes Kapital, Hauptberuf, Dokumentation |
| DAC8-Abgleich zeigt hohes Bruttovolumen | sicher ab 2027 | Steuerreport, der die Meldung erklärt; jährlich erklären |
| Gesetzesänderung (25 % Abgeltungsteuer ab 2027) | offen | Lots nach Anschaffungsdatum trennen |
| LLM-Advisor verschlechtert Ergebnis | mittel | Schattenmodus, nur Gate, harte Limits in Code |
| Kosten (GPU-Strom für vLLM, Steuertool, Zeit) übersteigen Gewinn | bei 1.000 EUR Kapital wahrscheinlich | Budget: Strom des GPU-Hosts (nur bei Bedarf einschalten oder stündlich kurz laufen lassen), 49 bis 99 EUR/Jahr Steuertool, deine Zeit. Bei 1.000 EUR Kapital sind 5 % Jahresrendite 50 EUR; das Projekt ist ein Lernsystem |
| Home-Lab-Ausfall (Container, Internet) bei offener Position | mittel | Bei Bitvavo kein Börsen-Stop möglich (Freqtrade-Doku: "not available"); Position liegt wie Buy-and-Hold, liegende Orders bleiben aktiv (Abschnitt 8.1). Minderung: Autostart, Watchdog, Alarm binnen 3 Minuten, Börsen-App für Notverkauf. Bei Kraken/OKX `stoploss_on_exchange` aktivieren und live mit Mini-Position testen, weil der Dry-Run keine Börsen-Stops platziert |
| Stromausfall | mittel | USV mit NUT und geordnetem Shutdown des Hosts, `--onboot 1` und `--startup order=` für den Container, `Restart=always` für den Bot (Abschnitt 8, Schritte 1, 5, 14) |
| Teilausführung bleibt als Mini-Position stehen | niedrig | `unfilledtimeout` sinnvoll setzen; `ledger.py` bucht jeden Fill einzeln; Restposition unter 5 EUR kann nicht verkauft werden und muss beim nächsten Trade mitgehen |

## 11. Umsetzungsphasen

| Phase | Dauer | Inhalt | Go-Kriterium für die nächste Phase |
|---|---|---|---|
| 0 Vorbereitung | Woche 1 bis 2 | Bitvavo-Konto, KYC, View-Key für Dry-Run; LXC, Tailscale, USV, Backup, Monitoring; Freqtrade installiert; Git-Repo; BTC/EUR-Historie aus Kraken-CSVs geladen | Alle Alarme getestet (Heartbeat-Ausfall, Neustart, Stromausfall-Simulation), Backup wiederhergestellt |
| 1 Backtest | Woche 2 bis 4 | BtcTrend-Strategie, Backtest 2017 bis 2026 auf Kraken-Daten mit Bitvavo-Stufe-0-Gebühren + 5 bps, Hyperopt mit Konfigurationszähler, Lookahead/Recursive-Analyse, Claude reviewt Code und Ergebnisse | Strategie überlebt 2018 und 2022, OOS-Jahr nicht schlechter als B&H bei geringerem Max-Drawdown, < 45 Konfigurationen |
| 2 Dry-Run | >= 3 Monate | `dry_run: true`, `dry_run_wallet` = Zielkapital, eigene DB; wöchentlicher Vergleich Dry-Run vs. Backtest; Steuer-Ledger und Dashboard laufen mit | Kein ungeklärter Drift, keine Störfälle, Ledger stimmt mit Börsen-Export überein |
| 3 Advisor-Schatten (optional) | parallel zu 2, >= 6 Monate | vLLM-Advisor loggt Entscheidungen (`ADVISOR_MODE=shadow`), keine Wirkung; Vergleich gated vs. ungated | Gated-Variante mindestens gleich gut nach Kosten; sonst Advisor streichen |
| 4 Live klein | 3 bis 6 Monate | 200 bis 500 EUR, Trade-Key anlegen, `max_open_trades 1`, fester Stake, Hard-Stop; Dry-Run läuft als Kontrollinstanz weiter; Teilausführung, Restart mit liegender Order und (bei Kraken/OKX) Börsen-Stop mit Mini-Position testen | Netto-Rendite pro Drawdown >= B&H im gleichen Fenster, Gebührenabfluss wie geplant, keine Abgleichfehler |
| 5 Skalieren | ab Monat 9 bis 12 | Kapital stufenweise erhöhen, Steuerberater prüft ersten Jahresreport | Weiterhin Kriterien aus Phase 4; sonst zurück auf Phase 2 oder Projekt als Lernprojekt abschließen |

No-Go jederzeit: Kill-Switch ausgelöst, unerklärte Bilanzdifferenz, Börse mahnt Ordermuster ab, Referentenentwurf ändert Kalkulation grundlegend.

## 12. Offene Entscheidungen

Stand 15.09.2026, Entscheidungen von Thomas: Hauptbörse Bitvavo; Startkapital 1.000 EUR; statische öffentliche IP vorhanden (API-Whitelist möglich); Proxmox VE 9.2.10 mit ZFS; USV vorhanden; Advisor lokal über vLLM. Die Tabelle zeigt den jeweiligen Stand.

| Frage | Entscheidung / Empfohlener Default |
|---|---|
| Hauptbörse Bitvavo oder OKX Europe? | Entschieden: Bitvavo |
| Börsen-Stop nötig? | Nein für die Tageskerzen-Strategie mit USV, Autostart und Alarm. Wenn du einen Stop auf der Börse willst, kippt die Börsenwahl zu Kraken (teurer) oder OKX (ungeprüft), weil Freqtrade `stoploss_on_exchange` bei Bitvavo nicht bietet |
| Börsenseitiger Dead-Man's-Switch (`cancelOrdersAfter`)? | Aus. Storniert auch Exit-Orders. Erst in Phase 4 mit Mini-Position bewerten |
| Zweitkonto bei Kraken? | Ja, aber erst in Phase 4, als Fallback |
| Zielkapital? | Entschieden: 1.000 EUR Startkapital (`dry_run_wallet` 1000, `START_CAPITAL_EUR` 1000). Live-Start in Phase 4 mit 200 bis 500 EUR davon, danach der Rest |
| Handelspaar? | BTC/EUR; BTC/USDC trotz 0,10 % RT verworfen (Abschnitt 4.2); Gebührenwährung aus `feeCurrency` je Fill prüfen |
| DCA-Bein? | Nur virtuell in `ledger.py` als Benchmark; echte DCA-Käufe nie im Bot-Konto |
| Entscheidungsintervall? | Tageskerzen mit UTC-Schluss 00:00 (alternativ 4h); Polling 1 bis 5 Minuten nur für Monitoring |
| Strategie? | 200-Tage-SMA-Trendfilter + Vol-Targeting, wöchentliches DCA als Benchmark |
| Advisor überhaupt? | Entschieden: ja, lokal über vLLM, gebaut in Phase 0; läuft zunächst nur im Schattenmodus (`ADVISOR_MODE=shadow`) |
| Modell für den Advisor? | Offen: hängt vom VRAM des GPU-Hosts ab, Empfehlungen in `docs/VLLM_ADVISOR.md`; Name in `ADVISOR_MODEL` eintragen |
| GPU-Host für vLLM? | Offen: welche Maschine, welche GPU, welche IP; muss vom Container auf Port 8000 erreichbar sein |
| Claude Code für Reviews? | Manuell per Sitzung auf dem Repo; Routines optional, wenn ein passender claude.ai-Plan vorhanden ist |
| Statische IP vorhanden? | Entschieden: ja; alle Bitvavo-Keys mit IP-Whitelist anlegen |
| Proxmox-Version und Storage? | Entschieden: PVE 9.2.10, rootfs auf ZFS (vzdump-Snapshot-Modus möglich) |
| USV? | Entschieden: vorhanden; NUT-Anbindung nach `deploy/proxmox/nut/README.md` |
| Dashboard? | FreqUI + kleine Ergänzungsseite; Grafana erst ab Phase 5 |
| Benachrichtigung? | Telegram + ntfy (self-hosted), Healthchecks als Dead-Man's-Switch |
| Steuertool? | Eigener FIFO-Report aus dem Ledger + Blockpit-Tier passend zur Trade-Zahl als Gegenprobe |
| Steuerberater? | Ja, vor der ersten Erklärung mit Bot-Trades und vor Jahresende 2026 wegen des Referentenentwurfs; auch Werbungskosten-Ansatz und Fristgrenzfall (Jahrestag) klären |
| Handy: iOS oder Android? | Bei iOS und self-hosted ntfy `upstream-base-url` akzeptieren oder nur Telegram nutzen |
| Repo-Hosting für Claude-Reviews? | Privates GitHub-Repo ohne Secrets; wöchentliche Claude-Routine öffnet PRs |

## 13. Quellen

1. ESMA, List of MiCA grandfathering periods Art. 143(3): https://www.esma.europa.eu/sites/default/files/2024-12/List_of_MiCA_grandfathering_periods_art._143_3.pdf
2. ESMA, Statement on MiCA transitional measures (04.12.2025): https://www.esma.europa.eu/sites/default/files/2025-12/ESMA75-113276571-1631_Statement_on_end_of_MiCA_transitional_periods.pdf
3. MiCA, Verordnung (EU) 2023/1114 (EUR-Lex): https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32023R1114
4. BaFin, Merkblatt Kryptowerte-Dienstleistungen nach MiCAR (03.01.2025): https://www.bafin.de/SharedDocs/Veroeffentlichungen/DE/Merkblatt/mb_250103_Kryptowerte_Dienstl.html
5. § 46 KMAG: https://www.gesetze-im-internet.de/kmag/__46.html
6. § 47 KMAG: https://www.gesetze-im-internet.de/kmag/__47.html
7. § 1 KWG: https://www.gesetze-im-internet.de/kredwg/__1.html
8. BT-Drs. 20/10280 (FinmadiG): https://dserver.bundestag.de/btd/20/102/2010280.pdf
9. Kryptowerte-Steuertransparenzgesetz (KStTG): https://www.gesetze-im-internet.de/ksttg/BJNR1600B0025.html
10. EU AI Act, Art. 2: https://artificialintelligenceact.eu/article/2/
11. ESMA, Supervisory Briefing on Algorithmic Trading (26.02.2026): https://www.esma.europa.eu/sites/default/files/2026-02/ESMA74-1505669079-10311_Supervisory_Briefing_on_Algorithmic_Trading_in_the_EU.pdf
12. btc-echo, MiCA-Aus Binance: https://www.btc-echo.de/news/mica-aus-das-muessen-binance-nutzer-jetzt-wissen-233343/
13. btc-echo, Nimmt Binance heimlich Neukunden auf: https://www.btc-echo.de/news/nimmt-binance-heimlich-in-der-eu-neukunden-auf-236510/
14. Trade Republic Kundenvereinbarung (Stand 7/2026): https://assets.traderepublic.com/assets/files/CA_DE-de.pdf
15. One Trading Instruments API: https://api.onetrading.com/fast/v1/instruments
16. Bitvavo Gebühren: https://bitvavo.com/en/fees
17. Bitvavo Rate Limits: https://docs.bitvavo.com/docs/rate-limits/
18. Bitvavo Market Fees API: https://docs.bitvavo.com/docs/rest-api/get-market-fees/
19. Bitvavo API Keys (Support): https://support.bitvavo.com/hc/en-us/articles/4405059841809-What-are-API-keys-and-how-do-I-create-them
20. Bitvavo Trading Rules: https://bitvavo.com/en/trading-rules
21. Bitvavo MiCA-Lizenz: https://bitvavo.com/en/news/bitvavo-obtains-mica-licence
22. Kraken, Cross-platform fee tier changes (Juli 2026): https://support.kraken.com/articles/cross-platform-fee-tier-changes
23. Kraken Fee Schedule: https://www.kraken.com/features/fee-schedule
24. Kraken, Updates for clients in Germany: https://support.kraken.com/articles/updates-for-clients-in-germany
25. Kraken EEA Terms: https://www.kraken.com/legal/eea-terms
26. Kraken API Keys: https://support.kraken.com/articles/how-to-create-an-api-key-on-kraken-pro
27. Kraken Spot Rate Limits: https://docs.kraken.com/api/docs/guides/spot-ratelimits/
28. Kraken API-Testumgebung: https://support.kraken.com/hc/en-us/articles/360000919926-Does-Kraken-offer-an-API-test-environment-
29. Kraken Verification levels: https://support.kraken.com/articles/201352206-verification-level-requirements
30. OKX Europe Fees: https://www.okx.com/en-eu/fees
31. Bitstamp API: https://www.bitstamp.net/api/
32. Bitstamp MiCA-Lizenz: https://blog.bitstamp.net/post/bitstamp-secures-casp-license-under-mica/
33. Bitpanda API Terms v1.1.0: https://cdn.bitpanda.com/terms-and-conditions/api-terms-bitpanda-bitpanda-gmbh-en-1.1.0.pdf
34. Bitpanda Fusion Terms: https://cdn.bitpanda.com/terms-and-conditions/advanced-trading-bitpanda-bitpanda-gmbh-en-1.0.0.pdf
35. Coinbase EEA User Agreement: https://assets.ctfassets.net/o10es7wu5gm1/69f3BVnUTRgArQ8PalNZm8/08bf839897a08fc8672a723c9e616dd4/Coinbase_EEA_User_Agreement_July_2025.pdf
36. Coinbase Trading Rules: https://www.coinbase.com/legal/trading_rules
37. Coinbase Advanced Trade Sandbox: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox
38. ccxt: https://github.com/ccxt/ccxt
39. BMF-Schreiben Kryptowerte (06.03.2025): https://www.bundesfinanzministerium.de/Content/DE/Downloads/BMF_Schreiben/Steuerarten/Einkommensteuer/2025-03-06-einzelfragen-kryptowerte-bmf-schreiben.pdf?__blob=publicationFile&v=2
40. § 23 EStG: https://www.gesetze-im-internet.de/estg/__23.html
41. § 32a EStG: https://www.gesetze-im-internet.de/estg/__32a.html
42. § 10d EStG: https://www.gesetze-im-internet.de/estg/__10d.html
43. § 35 EStG: https://www.gesetze-im-internet.de/estg/__35.html
44. § 11 GewStG: https://www.gesetze-im-internet.de/gewstg/__11.html
45. § 3 SolzG: https://www.gesetze-im-internet.de/solzg_1995/__3.html
46. BFH X R 7/99 (30.07.2003): https://www.judicialis.de/Bundesfinanzhof_X-R-7-99_Urteil_30.07.2003.html
47. BFH X R 1/97 (20.12.2000): https://dejure.org/dienste/vernetzung/rechtsprechung?Text=X+R+1%2F97
48. BFH IX R 29/19 (28.07.2021): https://www.bundesfinanzhof.de/de/entscheidung/entscheidungen-online/detail/STRE202110212/
49. FG Nürnberg 3 K 760/22 (22.01.2025): https://www.gesetze-bayern.de/Content/Document/Y-300-Z-BECKRS-B-2025-N-7241
50. BMF Wachstumsbooster: https://www.bundesfinanzministerium.de/Content/DE/Standardartikel/Themen/Steuern/Wachstumsbooster/wachstumsbooster.html
51. Winheller, Krypto-Trading-GmbH: https://winheller.com/blog/krypto-trading-gmbh-unternehmensbesteuerung/
52. btc-echo, Krypto-Haltefrist soll fallen (09.09.2026): https://www.btc-echo.de/news/krypto-haltefrist-soll-fallen-klingbeil-legt-gesetzentwurf-vor-237400/
53. krypto-besteuern.de, Referentenentwurf 2027: https://www.krypto-besteuern.de/blog/neue-besteuerung-kryptowerte-2027-referentenentwurf/
54. krypto-besteuern.de, Trading Bots: https://www.krypto-besteuern.de/blog/trading-bots-steuerrechtliche-wurdigung/
55. Steuertarif 2026: https://www.lawnet.de/service/mandantennews/dezember_2025/steuertarif_2026/
56. Blockpit Preise: https://www.blockpit.io/de-de/preise
57. CoinTracking vs Blockpit: https://cointracking.info/de/blog/cointracking-vs-blockpit/
58. Freqtrade Releases: https://github.com/freqtrade/freqtrade/releases
59. Freqtrade Configuration: https://www.freqtrade.io/en/stable/configuration/
60. Freqtrade Exchanges: https://www.freqtrade.io/en/stable/exchanges/
61. Freqtrade Advanced Setup (systemd, Logging): https://www.freqtrade.io/en/stable/advanced-setup/
62. Freqtrade Strategy Callbacks: https://www.freqtrade.io/en/stable/strategy-callbacks/
63. Freqtrade REST API: https://www.freqtrade.io/en/stable/rest-api/
64. Freqtrade FreqUI: https://www.freqtrade.io/en/stable/freq-ui/
65. Freqtrade Backtesting: https://www.freqtrade.io/en/stable/backtesting/
66. Freqtrade Telegram: https://www.freqtrade.io/en/stable/telegram-usage/
67. Freqtrade FreqAI: https://www.freqtrade.io/en/stable/freqai/
68. Proxmox Forum, Docker integration (Staff, 11/2025): https://forum.proxmox.com/threads/docker-integration.175870/
69. Proxmox Forum, Docker in LXC runc-Regression: https://forum.proxmox.com/threads/docker-inside-lxc-net-ipv4-ip_unprivileged_port_start-error.175437/
70. Proxmox Container-Doku: https://pve.proxmox.com/pve-docs/chapter-pct.html
71. Proxmox vzdump: https://pve.proxmox.com/pve-docs/chapter-vzdump.html
72. Proxmox Forum, LXC time sync: https://forum.proxmox.com/threads/proxmox-lxc-and-time-sync.78476/
73. Tailscale in unprivileged LXC: https://tailscale.com/docs/features/containers/lxc/lxc-unprivileged
74. Tailscale Serve: https://tailscale.com/kb/1312/serve
75. Healthchecks: https://github.com/healthchecks/healthchecks
76. Uptime Kuma: https://github.com/louislam/uptime-kuma
77. ntfy iOS instant notifications: https://docs.ntfy.sh/config/#ios-instant-notifications
78. Telegram Bots FAQ: https://core.telegram.org/bots/faq
79. Anthropic Glossar (Nicht-Determinismus): https://platform.claude.com/docs/en/about-claude/glossary
80. Anthropic Pricing: https://platform.claude.com/docs/en/about-claude/pricing
81. Claude Code Routines: https://code.claude.com/docs/en/routines
82. Protos, Alpha Arena Ergebnis (04.11.2025): https://protos.com/llm-crypto-trading-contest-finds-llms-cant-trade-crypto/
83. Business Standard / Bloomberg, Alpha Arena 1.5 (07.05.2026): https://www.business-standard.com/markets/news/ai-bots-auditioning-for-wall-street-trading-are-mostly-losing-money-126050701793_1.html
84. TradeRank: https://www.traderank.ai/
85. When Agents Trade (arXiv 2510.11695): https://arxiv.org/abs/2510.11695
86. Agentic Trading Survey (arXiv 2605.19337): https://arxiv.org/abs/2605.19337
87. Systematic Trend-Following (arXiv 2602.11708): https://arxiv.org/html/2602.11708v1
88. Dynamic Grid Trading (arXiv 2506.11921): https://arxiv.org/abs/2506.11921
89. Bailey et al., Pseudo-Mathematics and Financial Charlatanism: https://www.ams.org/notices/201405/rnoti-p458.pdf
90. Chague et al., Day Trading for a Living? (Crossref): https://api.crossref.org/works/10.2139/ssrn.3423101
91. Barber/Odean, Trading is Hazardous to Your Wealth: https://faculty.haas.berkeley.edu/odean/papers/returns/individual_investor_performance_final.pdf
92. BIS WP 1049: https://www.bis.org/publ/work1049.htm
93. Man Group, Crypto: Too Hot to Handle?: https://www.man.com/insights/crypto-too-hot-to-handle
94. Concretum, Catching Crypto Trends: https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/
95. Grafana PostgreSQL Datasource: https://grafana.com/docs/grafana/latest/datasources/postgres/
96. TimescaleDB PG-Support: https://www.tigerdata.com/docs/self-hosted/latest/upgrades/upgrade-pg
97. InfluxDB 3 Core Config: https://docs.influxdata.com/influxdb3/core/reference/config-options/
98. QuantStats: https://github.com/ranaroussi/quantstats
99. SQLite, When to use: https://www.sqlite.org/whentouse.html
100. htmx Docs: https://htmx.org/docs/
101. Freqtrade Stoploss (stoploss_on_exchange): https://www.freqtrade.io/en/stable/stoploss/
102. Freqtrade Exchange-Features-Tabelle (Bitvavo "not available"): https://raw.githubusercontent.com/freqtrade/freqtrade/stable/docs/includes/exchange-features.md
103. Freqtrade Plugins / Protections: https://www.freqtrade.io/en/stable/plugins/
104. Freqtrade Data Download (Kraken CSV, trades-to-ohlcv): https://www.freqtrade.io/en/stable/data-download/
105. Freqtrade Quellcode, Retry-Logik (`API_RETRY_COUNT`): https://raw.githubusercontent.com/freqtrade/freqtrade/stable/freqtrade/exchange/common.py
106. Freqtrade Quellcode, Dry-Run-Stops und Precision (`exchange.py`): https://raw.githubusercontent.com/freqtrade/freqtrade/stable/freqtrade/exchange/exchange.py
107. Freqtrade Quellcode, Startup-Abgleich und Partial Fills (`freqtradebot.py`): https://raw.githubusercontent.com/freqtrade/freqtrade/stable/freqtrade/freqtradebot.py
108. Freqtrade Quellcode, Verhalten bei TemporaryError/OperationalException (`worker.py`): https://raw.githubusercontent.com/freqtrade/freqtrade/stable/freqtrade/worker.py
109. Freqtrade Unit-Vorlage `freqtrade.service.watchdog`: https://raw.githubusercontent.com/freqtrade/freqtrade/stable/freqtrade.service.watchdog
110. Bitvavo Get Account (Gebührenstufe, `GET /v2/account`): https://docs.bitvavo.com/docs/rest-api/get-account-fees/
111. Bitvavo Get Markets (Live-Abfrage BTC-EUR): https://api.bitvavo.com/v2/markets?market=BTC-EUR
112. Bitvavo Create Order (`clientOrderId`, `postOnly`, `selfTradePrevention`, `operatorId`, `feeCurrency`): https://docs.bitvavo.com/docs/rest-api/create-order/
113. Bitvavo Get Trade History (`GET /v2/trades`, Fills mit `fee`, `feeCurrency`): https://docs.bitvavo.com/docs/rest-api/get-trade-history/
114. Bitvavo Cancel Orders After (Dead-Man's-Switch): https://docs.bitvavo.com/docs/rest-api/cancel-orders-after/
115. Bitvavo Support, Handelsgebühren und Gebührenreservierung bei Limit-Orders: https://support.bitvavo.com/hc/en-us/articles/4405175148689
116. Bitvavo Support, Euro-Auszahlungen und Auszahlungslimit (nur als Suchauszug lesbar): https://support.bitvavo.com/hc/en-us/articles/4405188690065
117. Kraken CancelAllOrdersAfter (Dead-Man's-Switch): https://docs.kraken.com/api/docs/rest-api/cancel-all-orders-after
118. ccxt Bitvavo-Implementierung (`cancelAllOrdersAfter`): https://raw.githubusercontent.com/ccxt/ccxt/master/ts/src/bitvavo.ts
119. Jay Azhang (Nof1), X-Post zum Ende von Alpha Arena Season 1 (03.11.2025): https://x.com/jay_azhang/status/1985481491078328621
120. Kaiko, The State of the European Crypto Market 2025-2026 (Mai 2026, von Bitvavo bezahlt): https://resources.kaiko.com/hubfs/Research/The%20State%20of%20the%20European%20Crypto%20Market%20-%202025%20-%202026%20Trends%20in%20Trading%20Activity.pdf
121. Deribit DVOL API (`get_volatility_index_data`): https://docs.deribit.com/#public-get_volatility_index_data
122. § 90 AO (Mitwirkungspflichten): https://www.gesetze-im-internet.de/ao_1977/__90.html
123. § 162 AO (Schätzung): https://www.gesetze-im-internet.de/ao_1977/__162.html
124. § 108 AO (Fristen, Verweis auf §§ 187 ff. BGB): https://www.gesetze-im-internet.de/ao_1977/__108.html
125. Tailscale, Enabling HTTPS (MagicDNS, Certificate Transparency): https://tailscale.com/kb/1153/enabling-https
126. Tailscale, Firewall-Ports: https://tailscale.com/kb/1082/firewall-ports
127. Proxmox `pct` Manpage (`pct create`, `--onboot`, `--startup`, `--dev0`): https://pve.proxmox.com/pve-docs/pct.1.html
128. NUT User Manual, Configuration notes (upsmon, SHUTDOWNCMD): https://networkupstools.org/docs/user-manual.chunked/Configuration_notes.html
129. journald.conf (`SystemMaxUse`, `MaxRetentionSec`): https://man7.org/linux/man-pages/man5/journald.conf.5.html
