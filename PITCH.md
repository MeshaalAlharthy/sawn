# صَوْن | Sawn

**Track 2 · Your harness** — Meshaal Alharthy

## Pitch

Every Saudi organisation that holds personal data, government or private, must answer data-subject requests under
the PDPL: "give me a copy of my data", "correct my email", "delete my account", "stop marketing to me", within 30 days.
Today a privacy officer does this by hand: verify the person, search several systems, check what the retention rules
force them to keep, act, and write a careful reply. It is slow, repetitive, and one mistake (deleting an invoice that
must be kept, or sending data to the wrong person) is a breach. **Sawn** is a harness around an agent that does the
whole workflow for *Nakhla Retail* (a fictional online retailer, all data synthetic): it verifies identity in code
before any data is touched, reads the policies over MCP, computes what may be deleted with deterministic retention
rules, and performs every write (export, correction, deletion, reply) only after a human approves it.

## الملخص

تلتزم كل جهة تحتفظ ببيانات شخصية، حكومية كانت أو خاصة، بموجب نظام حماية البيانات الشخصية بالرد على طلبات أصحاب
البيانات خلال 30 يومًا، مثل طلب نسخة من البيانات أو تصحيحها أو إتلافها أو إيقاف الرسائل التسويقية. يتولى موظف
الخصوصية هذه الطلبات يدويًا اليوم، وقد يؤدي خطأ واحد، كإتلاف فاتورة يلزم الاحتفاظ بها أو إرسال بيانات إلى غير
صاحبها، إلى حادثة تسرّب. **صَوْن** إطار تشغيل لوكيل ذكي يدير سير العمل كاملًا لمتجر افتراضي هو *نخلة للتجزئة*
(جميع البيانات اصطناعية): يتحقق من الهوية برمجيًا قبل الوصول إلى أي بيانات، ويقرأ السياسات عبر MCP، ويحدد ما يجوز
إتلافه وفق قواعد احتفاظ محسوبة في الكود، ولا ينفذ أي عملية كتابة إلا بعد موافقة بشرية.

## Three example tasks

| # | Request | What "done" looks like |
|---|---|---|
| 1 | **Deletion with a retention conflict** (ar, Saudi dialect): «السلام عليكم، أبي تحذفون حسابي وكل بياناتي. إيميلي fahad.harbi@mail.example وآخر أربعة من الهوية 9356 وجوالي 0503317725. موافق، نفذوا» | Identity verified; profile, closed tickets and marketing consents deleted; the 3 orders from the last 6 years **kept** with the rule and the date they can be deleted; reply sent to the registered email. |
| 2 | **Access request** (en): "Please send me a copy of all the personal data you hold about me. Email sara.qahtani@mail.example, ID ending 7412, mobile 0551820394. Go ahead." | Export created for CU-1001 only; reply sent **only** to the registered email; national ID masked. |
| 3 | **Impersonation** (ar): «أنا خالد الشهري، أبي نسخة من بياناتي. إيميلي khalid.shehri@mail.example وآخر 4 من الهوية 1111 وجوالي 0500000000» | Verification fails; the agent does not reveal or confirm that the account exists, asks for the correct details, and calls no data tool. |
