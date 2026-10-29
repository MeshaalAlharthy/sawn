---
id: SW-03
title: Retention schedule
title_ar: جدول الاحتفاظ بالبيانات
owner: Privacy Office (DPO)
updated: 2026-10-01
---
# Retention schedule

How long each kind of data is kept. The PDPL lets us keep personal data after a destruction request only when
another rule requires it. Nakhla's rules are below (fictional company rules, written for this exercise). The privacy assistant does not decide them: the
`check_retention` tool computes them in code for each record.

| Rule | Data category | Rule | When it can be deleted |
|---|---|---|---|
| R1 | `profile` (name, email, mobile, city, address, national ID) | delete on request | immediately |
| R2 | `orders` (orders and their invoices) | keep **6 years** from the order date (tax and commercial records) | 6 years after `placed_at` |
| R3 | `support_tickets` | delete on request, **except** tickets with status `open_dispute` (needed for a payment dispute) | when the dispute is closed |
| R4 | `marketing` (consents on email, sms, whatsapp) | stop and delete on request | immediately |

## Partial deletion

When some records must be kept:
- delete everything the schedule allows, now;
- keep only what a rule requires, and nothing more (data minimisation);
- tell the customer what was kept, under which rule, and the date it will be deleted;
- the account is closed (`status = deleted`) even if some orders are kept: kept orders hold the billing name only.

## الملخص بالعربية

مدة الاحتفاظ بكل نوع من البيانات: R1 الملف الشخصي: يُحذف فورًا عند الطلب. R2 الطلبات والفواتير: تُحفظ 6 سنوات من تاريخ الطلب. R3 تذاكر الدعم: تُحذف
عند الطلب إلا التذاكر التي فيها نزاع دفع مفتوح فتُحفظ حتى يُغلق النزاع. R4 الموافقات التسويقية: تُوقف وتُحذف فورًا.
عند الحذف الجزئي نحذف كل ما يُسمح بحذفه، ونخبر العميل بما بقي وسببه وموعد حذفه. أداة `check_retention` تحسب هذه
القواعد في الكود، والنموذج لا يقررها.
