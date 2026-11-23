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