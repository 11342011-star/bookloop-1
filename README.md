# BookLoop

## Run locally

The ISBN lookup uses a small same-origin Python endpoint to query Google Books
and Open Library without browser cross-origin restrictions. Start the app with:

```sh
python3 server.py
```

Then open `http://localhost:4173`. For phone camera access, use a secure HTTPS
forwarded URL and grant camera permission.

The nearest-station demo combines university/college and MRT coordinates from
Wikidata. It assumes every campus and MRT station has a book box and shows the
10 closest combined results; coordinates are illustrative, not verified box
locations. The book creation form includes photo/video capture and previews.
Media is kept in browser memory only: it is attached to the book in the current
page session, but is not uploaded or saved after a refresh.

## Wishlist and transaction prototype

Book listings can be stored in Supabase and shared across browsers; the shared
catalog feeds both the library and search results. The frontend uses the
publishable key in `supabase-config.js`, which is public by design. Never put a
secret/service-role key in the frontend. Run `supabase_schema.sql` in the
Supabase SQL Editor to create the catalog table and its read/insert RLS
policies. Run the current `supabase_schema.sql` in the Supabase SQL Editor to
create the shared catalog and transaction-status tables; rerun it after schema
updates. Catalog and transaction status are public and can be read or changed
anonymously, so this is suitable for the current prototype, not a production
marketplace. When Supabase is not configured, local development can use the
Python API in SQLite instead. Transaction status metadata syncs across browsers,
but point top-ups, balances, detailed orders, chat, and wishlists remain
browser-local in `localStorage`. Local demo accounts have separate IDs, but
these IDs are not verified, do not authenticate real Google/Apple accounts, and
are not shared between browsers or devices.
Built-in sample books are local examples, so orders for them stay local; only
books confirmed in the Supabase shared catalog create shared transaction
records.
Each local demo account receives 150 starter points once, after its first
successful book listing. Listing points use the backend rules-based estimator:
ISBN validity, catalog fields, condition, evidence, and previous circulations
affect the score; this is not an AI evaluation. Book trades offer book-for-book, points, and cash
options; the prototype cash amount reuses the book's points value at NT$1 per
point and never collects real money. Buyers can specify a convenience-store
branch or choose an illustrative campus/MRT book-box location. Book-box
coordinates do not confirm an actual deployed box. Top-up, payment, delivery,
and pickup/cooldown controls are simulations and do not charge money or contact
a convenience-store provider. The prototype starts the 72-hour seller-point
hold after its simulated delivery-completion event, and only records
circulation rewards after settlement.

The shared transaction table stores book title/author, demo buyer/seller IDs,
trade mode, estimated points, delivery method, and status; it does not store a
specific store/box address or chat/payment details. Because demo IDs and
anonymous status updates are not authenticated, users can impersonate accounts
or alter transaction status. Do not use this prototype state as proof of
ownership, payment, pickup, or fulfillment. Production transactions require a
backend, real user authentication, a payment/top-up provider, and authenticated,
verified C2C fulfillment webhooks. Client-side controls must never be treated
as payment or pickup proof.

## Private platform analytics (prototype)

The platform dashboard is served at **其他功能 → 平台數據管理**. It requires a
server-side admin password and an authenticated, HttpOnly session cookie; the
password must never be embedded in `index.html` or committed to the repository.
Set a unique password of at least 16 characters before starting the server:

```sh
export BOOKLOOP_ADMIN_PASSWORD='replace-with-a-long-random-secret'
export BOOKLOOP_ANALYTICS_DB='/path/to/private-data/bookloop-analytics.sqlite3'
python3 server.py
```

`BOOKLOOP_ANALYTICS_DB` is optional and defaults to
`bookloop_analytics.sqlite3` in the project directory. Keep the database on a
persistent, access-controlled disk and back it up securely. The built-in
`python3 server.py` listener is for local/development use; production should put
the app behind HTTPS and a maintained production web server, set
`X-Forwarded-Proto: https` only from a trusted proxy, and restrict network
access to the service. GitHub Pages alone cannot run this backend or protect
the dashboard; serve the frontend and `/api` from the same trusted origin.

The current sign-in, account balances, detailed orders, and fulfillment remain
browser-local simulations; only listing records and basic transaction status
are shared through Supabase. Analytics events are anonymous, client-reported
prototype signals—not verified customers, authoritative inventory, or real
transaction records. The ESG panel uses an illustrative 1.02 kg CO₂e per
simulated settled circulation, not a measured or independently verified
impact. Do not use these figures as production business or sustainability
claims. The CSV export contains event-level anonymous references and
book/trade fields; protect and handle downloaded files as private data.

## External bookstore links

When a BookLoop search has no in-stock match, users can search the title, author,
or ISBN on Books.com.tw, Eslite, Kingstone, and TAAZE. These are outbound search
links only; BookLoop does not fetch or guarantee retailer inventory or prices.
Affiliate IDs, click tracking, and paid-ad integrations are not configured in
this prototype.