# Staging checklist: KSA "05" phone rule on appointment forms (v1.0.52)

Manual checks on the odoo.sh staging build of `mainTest`, after `wayakit_customization`
upgrades (it now depends on `appointment_sms`). Use fake numbers only (`0512345678`,
`+52 55 1234 5678`); never a real customer's phone. Do not finish a booking unless you
cancel it afterwards.

Pure rule (no Odoo): `python3 wayakit_customization/tests/test_appointment_phone.py`.

## Phone 05 section

0. **Data check (pre-condition).** Back office > Appointments > Configuration: types 1 (Car
   Wash Care), 3 (HOME Deep Cleaning Care) and 15 (Al Khozama) have **Website** set to the
   KSA website (company id 1). If Website is empty ("all websites"), the type is treated as
   non-KSA and silently keeps the old rule. Type 2 (Mexico sales meeting) has no KSA website.
1. **Type 1 form** (`/appointment/1`, pick any slot, reach "Your data"):
   - phone placeholder is `05X XXX XXXX` (not `e.g. +1(605)691-3277`);
   - the lightbulb note "Please enter your correct mobile number..." shows under the field;
   - the hint "We will use it to remind you of this appointment." is **not** shown;
   - on mobile the keypad is numeric (`inputmode="numeric"`).
2. **Repeat step 1 for types 3 and 15** (`/appointment/3`, `/appointment/15`).
3. **Browser validation on type 1:** `+966512345678`, `0612345678`, `05-1234-5678` are each
   refused by the browser with the "Saudi mobile number starting with 05" tooltip.
   `05 1234 5678` is accepted by the form (stop before confirming, or cancel the booking).
4. **Type 2 form** (`/appointment/2`):
   - placeholder is `Phone number`; neither placeholder nor tooltip mentions `05`;
   - no "correct mobile number" note; the SMS "remind you" hint shows only if type 2 has an
     SMS reminder configured (unchanged native behaviour);
   - `+52 55 1234 5678` is accepted by the form; `1234567` is refused.
5. **Server guard (HTML pattern bypassed) on type 1:** on the "Your data" form open devtools,
   select the phone input and remove its `pattern` attribute (Elements panel, or Console:
   `document.querySelector('input[name=phone]').removeAttribute('pattern')`). Enter
   `+966512345678`, fill the rest with fake data and submit.
   Expected: redirect to `/appointment/1?...state=failed-phone...` with the red alert
   "Appointment failed! ... valid phone number ..." **plus** "It must be a Saudi mobile number
   starting with 05, e.g. 05X XXX XXXX." No calendar event / booking is created (check
   Calendar for that slot).
6. **Same bypass on type 2** with `1234567`: redirected to `failed-phone`, the alert does
   **not** include the "starting with 05" sentence.
7. **Arabic site:** open `/ar/appointment/1` and `/ar/appointment/3`, reach "Your data": the
   form renders without a server error, the 05 placeholder and the note show (note text may
   be in English until translated; placeholder/tooltip come from Python and stay English).
   Repeat the step 5 bypass on `/ar/appointment/1`: the failed-phone alert renders, with the
   "starting with 05" sentence (English until translated).
   Also check type 2 with ` 1234 567 ` (7 digits padded with spaces): the browser refuses it
   (pattern tightened so it never passes the form and then bounces on the server).
8. **Regression:** HOME Deep Cleaning (type 3) kitchen-area check still works: a valid
   `0512345678` with a mismatched kitchen option still lands on `failed-kitchen-area`, not
   `failed-phone`.
