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

The wishlist, local book matching, point top-ups, and order states are a
browser-local prototype persisted in `localStorage`; they are not synchronized
between users or devices. Matching is based on ISBN, title, and author rather
than an external AI service. Top-up methods and the pickup/cooldown controls
are simulations and do not charge money or contact a convenience-store
provider. The prototype starts the 72-hour seller-point hold after its simulated
paid-pickup event, and only records circulation rewards after settlement.

Production transactions require a backend, a real payment/top-up provider, and
authenticated, verified C2C fulfillment webhooks. Client-side controls must
never be treated as payment or pickup proof.

## External bookstore links

When a BookLoop search has no in-stock match, users can search the title, author,
or ISBN on Books.com.tw, Eslite, Kingstone, and TAAZE. These are outbound search
links only; BookLoop does not fetch or guarantee retailer inventory or prices.
Affiliate IDs, click tracking, and paid-ad integrations are not configured in
this prototype.