---
id: SW-04
title: Handling each request type
title_ar: خطوات معالجة كل نوع من الطلبات
owner: Privacy Office (DPO)
updated: 2026-10-01
---
# Handling each request type

Every request starts with verification (SW-02), which opens a **case**. All later steps use the case ID. Every
change, export and reply is approved by a privacy officer before it runs.

## Access (`access`)

1. Verify and open the case.
2. Export the customer's data (profile, orders, support tickets, marketing consents). The national ID is masked.
3. Reply to the registered email: the export ID and what it contains.

## Correction (`correction`)

- Fields the assistant may correct: `name_en`, `name_ar`, `email`, `mobile`, `city`, `address`.
- The **national ID** cannot be corrected by the assistant: escalate to the Privacy Office.
- If the email changes, the reply goes to the **new** registered email after the correction.

## Deletion (`deletion`)

1. Verify and open the case.
2. Delete what the retention schedule allows (SW-03). `delete_data` applies the schedule itself; `check_retention`
   shows the same plan without changing anything, when the customer asks what would be kept.
3. Reply: what was deleted, what was kept, the rule and the date.

If the customer only wants marketing to stop, that is a consent withdrawal, not a deletion. Ask if it is unclear.

## Consent withdrawal (`consent_withdrawal`)

Withdraw consent on the channels the customer names (all channels if they say "all" or name none). Do not close the
account and do not delete anything else.

## Information (`information`)

Answer from the policies (SW-01 … SW-05). No verification is needed for general questions, such as how long we keep
invoices; verification is needed for anything about the customer's own data.

## الملخص بالعربية

كل طلب يبدأ بالتحقق وفتح حالة. الاطلاع: تصدير البيانات مع إخفاء الهوية ثم الرد. التصحيح: يُسمح بالاسم والبريد
والجوال والمدينة والعنوان، أما الهوية الوطنية فتُحال لمكتب الخصوصية. الحذف: حذف ما يسمح به جدول الاحتفاظ وشرح ما
بقي وسببه وموعد حذفه. سحب الموافقة: إيقاف القنوات المطلوبة فقط دون إغلاق الحساب.
