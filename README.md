# Parkly auto-booker

Booker parkeringsplass hos Parkly automatisk for neste virkedag. Parkly legger ut plassene kl. 13:00 dagen før, og denne jobben står klar og booker i det sekundet de blir tilgjengelige.

Alt kjører gratis i din egen GitHub-konto. Ingen andre ser passordet ditt.

## Oppsett (ca. 10 minutter)

### 1. Lag din egen kopi

Klikk den grønne knappen **Use this template → Create a new repository** øverst på denne siden.

- Repository name: `parkly-autobooker`
- Velg **Private** (viktig)
- Klikk **Create repository**

Resten av stegene gjør du i **din kopi**.

### 2. Finn assignment-nummeret ditt

1. Logg inn på [app.parkly.no](https://app.parkly.no) i Chrome eller Edge.
2. Trykk **F12** og åpne fanen **Network**.
3. Book en dag manuelt i Parkly.
4. I lista dukker det opp et kall som heter `assignment/` og et tall, for eksempel `assignment/12345`. Det tallet er assignment-nummeret ditt.

### 3. Legg inn tre secrets

Gå til **Settings → Secrets and variables → Actions → New repository secret**, og lag disse tre, én om gangen:

| Name | Secret |
|---|---|
| `PARKLY_USERNAME` | e-posten du logger inn på Parkly med |
| `PARKLY_PASSWORD` | Parkly-passordet ditt |
| `PARKLY_ASSIGNMENT_ID` | tallet fra steg 2 |

### 4. Slå på og test

1. Gå til fanen **Actions**. Hvis du får spørsmål om å slå på workflows, klikk den grønne knappen.
2. Klikk **Parkly Auto Booker** til venstre.
3. Klikk **Run workflow** til høyre, velg **test-innlogging**, og trykk den grønne **Run workflow**.
4. Etter 1–2 minutter skal kjøringen være grønn. Under steget «Book parkering» står det «Innlogging OK».

Ferdig. Fra nå av booker den automatisk hver søndag–torsdag for neste virkedag.

## Slik fungerer det

1. Jobben starter ca. 12:10, 12:30 og 12:45. Tre starttider gir sikkerhet hvis GitHub er forsinket.
2. Den logger inn på Parkly, og sjekker om morgendagen allerede er booket.
3. Kl. 12:55 logger den inn på nytt, og kl. 12:59:57 sender den bookingen.
4. Hvis det ikke går, prøver den igjen: hvert sekund det første minuttet, så sjeldnere, helt til kl. 16:00.
5. Hvis bookingen ikke lykkes, blir jobben rød og GitHub sender deg e-post.

## Kjøre manuelt

**Actions → Parkly Auto Booker → Run workflow**, og velg:

- `test-innlogging` sjekker at brukernavn og passord virker
- `dry-run` viser hva som ville skjedd, uten å booke
- `book-naa` booker i morgen med en gang
- `vent-til-13` gjør det samme som den daglige jobben

## Greit å vite

- **Bruk av GitHub-minutter:** Jobben bruker ca. 50 minutter per hverdag, mest venting fram til 13:00. Det blir rundt 1100 minutter i måneden. Gratis GitHub-kontoer har 2000 minutter i måneden for private repoer.
- **60-dagersregelen:** GitHub slår av planlagte jobber etter 60 dager uten endringer i repoet. Du får e-post før det skjer. Da går du til **Actions** og klikker **Enable workflow**.
- **Byttet passord?** Oppdater `PARKLY_PASSWORD` under Settings → Secrets.
