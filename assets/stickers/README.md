# Sticker assets

Place the approved static sticker files directly in this directory as WebP:

```text
assets/stickers/agree.webp
assets/stickers/happy.webp
```

The filename without `.webp` becomes the permanent `sticker_id` stored in
discussion rows. Keep each id at 200 characters or fewer. Do not replace or
remove an id that has already been used; add a new filename when the artwork
changes so historical posts keep their original meaning.

For the repository-backed pilot, use static 256–320 px WebP images and aim for
less than 100 KB each. Images are requested only after an authorised user opens
the picker and are then cached by the browser using the application version.

Adding or changing files requires a normal application release/redeploy. Only
add artwork whose application and distribution rights have been confirmed.
