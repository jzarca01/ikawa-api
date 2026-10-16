# IKAWA Profiles API

Python (FastAPI) API to sign in to an IKAWA account and retrieve roast profiles and roast logs as JSON.

## Configuration

```bash
cp .env.example .env
```

```
# Firebase project settings (extracted from com.ikawacoffee.ikawapp.apk resources.arsc)
FIREBASE_API_KEY=
FIREBASE_PROJECT_ID=ikawa-app
```

## Installation

```bash
cd ~/Projets/ikawa-api
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
uvicorn app:app --reload --port 8000
```

## Endpoints

### POST /login

Email/password authentication.

```bash
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"your-password"}'
```

Returns an `idToken` to use for subsequent calls.

All the endpoints below require that token in an `Authorization: Bearer <idToken>` header. It is declared as a security scheme, so `/docs` shows an **Authorize** button: paste the `idToken` there once and every request will carry the header.

### GET /profiles

Retrieves all profiles of the account (paginated on the Firestore side).

```bash
curl http://localhost:8000/profiles \
  -H "Authorization: Bearer <idToken>"
```

Returns a JSON array of profiles with fields: uuid, name, coffeeName, roastTempPoints, roastFanPoints, isArchived, isFavourite, shareUrl, etc.

### POST /profiles/import

Imports a roast profile into the account from its IKAWA share URL (standard base64, URL-safe and unpadded variants are all accepted).

```bash
curl -X POST http://localhost:8000/profiles/import \
  -H "Authorization: Bearer <idToken>" \
  -H "Content-Type: application/json" \
  -d '{"shareUrl":"https://share.ikawa.support/profile/?CAESE..."}'
```

Reproduces the app's import flow: protobuf decoding (`RoasterHelper.decodeBase64toProfile` + `Profile.convertFromProtobuf` + `IkawaProfile.fromLibraryProfile`), then writes to Firestore (`FirestoreProfilesImpl.updateProfileAsync`, `SetOptions.merge()`) at `apps/pro/users/{uid}/roastProfiles/{uuid}`.

Returns the written document and the decoded profile. Details faithful to the app:
- `userID` = authenticated user's uid, `profileType` = 0 (USER), `revision` = 0
- `isArchived`/`isFavourite` = false, `showInLibrary` = true
- `dateLastRoasted` = epoch 0 (`new Date(0)` from `fromLibraryProfile`)
- if a profile with the same UUID already exists, only the provided fields are updated (merge), just like in the app

### POST /profiles/import/bulk

Imports a batch of share URLs into the account. An invalid URL does not fail the whole batch: every URL gets its own status in the response.

```bash
curl -X POST http://localhost:8000/profiles/import/bulk \
  -H "Authorization: Bearer <idToken>" \
  -H "Content-Type: application/json" \
  -d '{
    "shareUrls": [
      "https://share.ikawa.support/profile/?CAESE...",
      "https://share.ikawa.support/profile/?CAESF..."
    ],
    "maxConcurrency": 4,
    "skipDuplicates": true
  }'
```

Request body:

| Field | Type | Default | Description |
|---|---|---|---|
| `shareUrls` | list[str] | required | 1 to 500 URLs. A single string (newline/comma/space separated) is also accepted |
| `maxConcurrency` | int | 4 | Parallel workers, between 1 and 16 |
| `skipDuplicates` | bool | true | Skip URLs whose UUID appears twice in the same batch |

Response:

```json
{
  "total": 3,
  "imported": 2,
  "failed": 1,
  "duplicates": 0,
  "results": [
    {"index": 0, "status": "imported", "uuid": "9CEF...", "name": "Micro Africa Washed Start", "error": null},
    {"index": 1, "status": "imported", "uuid": "90A9...", "name": "La Esmeralda Geisha Washed 1", "error": null},
    {"index": 2, "status": "invalid_share_url", "uuid": null, "name": null, "error": "share URL contains no profile id"}
  ]
}
```

Per-URL statuses: `imported`, `invalid_share_url` (decoding failed), `duplicate_in_batch` (same UUID already imported in this batch), `firestore_error` (write rejected by Firestore, upstream error included).

## Roast log

Roast logs are not stored with profiles. The app keeps them in two separate Firestore collections (`FirestorePaths.kt`):

| Collection | Model | Content |
|---|---|---|
| `apps/pro/users/{uid}/roastDetailedLog/{roastID}` | `FirestoreRoast` | the roast itself: the recorded curve and roaster settings |
| `apps/pro/users/{uid}/roastNoteLogs/{roastID}` | `FirestoreRoastNotes` | your notes on it: coffee, first crack, dose, yield |

There is **no roast-log share URL**. `ShareProfileUtil` only encodes *profiles* (`getProfileShareUrl`); the roast export is a CSV attachment named `{roastID}.csv` containing `roastData` (`ShareRoastLogFromRoastDetailsUseCase`), and a roast only ever enters Firestore from a real roast on the machine (`FirestoreRoastLogsImpl.updateRoast`, a full `document(roastID).set(roast)`, no merge). So `/roastlog/import` takes the document itself rather than a URL.

### GET /roastlog

```bash
curl "http://localhost:8000/roastlog?parseRoastData=true" \
  -H "Authorization: Bearer <idToken>"
```

Reproduces `FirestoreRoastLogsImpl.getUserRoastLogs` / `getUserRoastNotes`:

- reads `roastDetailedLog` filtered on `dateLastEdited >= <lastEditedMillis>` (default `0`, i.e. everything);
- does **not** filter on `roasterID` by default — see below;
- skips any document whose envelope is malformed, like the app's per-document `try`/`catch` around `toObject()`;
- joins the matching `roastNoteLogs` document onto `notes` by `roastID`.

The app itself keeps only roasts whose `roasterID` parses as an int (`FirestoreRoast.getRoasterID()` returns `-1` otherwise). That filter is available as `?onlyRoastsWithRoasterID=true`, but it is **off by default** because in a real account it silently discards every roast whose `roasterID` is null — the whole oldest generation of logs. Note that where the app reads a small roaster id, older documents carry a machine identifier (e.g. `"580152"`), so the flag means "roasts carrying a roaster/machine id", not "roasts from a known machine".

### Two document schemas in one account

The current app writes `FirestoreRoast` (`roaster_config_code`, `userID`, `deviceID`), but real accounts also contain older documents written before that schema — with `roasterConfig` instead of `roaster_config_code`, plus `csvOnServer`, `dataOnServer`, `machineUUID`, `bluetoothID` and `wasUploadedToFirebaseEver`. Both are read: `roaster_config_code` is filled from whichever field is present, and the legacy fields are exposed as-is so nothing is hidden. `GET /roastlog/diagnose` shows the real field names and value types for an account.

`roastNoteLogs` documents carry more than the app's own model: `dtr`, `endTemp`, `extraInfo`, `preHeatPower`, `preHeatTemp`, `roastingTime`, `secondCrackTemp`, `secondCrackTime`, `creatorID`, `appDeviceID`, plus a `roastDetailedLog` reference to the roast document. `timeZoneOffset` is stored as a **double** even though the app models it as `Int`.

Imports always write the current app's field names, so imported roasts are readable by the app.

The read uses `documents:list` — the same call as `GET /profiles` — so the collection is addressed the same way (`apps/pro/users/{uid}` + the collection segment) and pages with `pageSize`/`pageToken`. It is deliberately **not** `:runQuery`: that method takes the document path as `RunQueryRequest.parent`, and the IKAWA layout (`apps/pro/users/{uid}/{collection}` is a 5-segment path) is rejected there as a malformed document parent. The `dateLastEdited` filter is therefore applied client-side. The resulting set matches the app's server-side `whereGreaterThanOrEqualTo("dateLastEdited", ...)`, including dropping documents with no `dateLastEdited`, which Firestore's own `>=` comparison also excludes.

Timestamps are parsed rather than compared as strings, so Firestore's nanosecond precision (`2024-05-01T10:20:30.123456789Z`) and explicit offsets are handled correctly.

| Query param | Type | Default | Description |
|---|---|---|---|
| `lastEditedMillis` | int | `0` | epoch **milliseconds**; only roasts edited at or after this instant |
| `parseRoastData` | bool | `false` | also decode `roastData` into `roastPoints` |
| `omitRawRoastData` | bool | `false` | drop the raw CSV once `roastPoints` is decoded — see below |
| `profileUUID` | str | — | only roasts referencing this profile — see below |

### Filter by profile

```bash
curl "http://localhost:8000/roastlog?profileUUID=90A99999888777666555443332221100" \
  -H "Authorization: Bearer <idToken>"
```

Keeps only the roasts whose `profileUUID` is that profile. Roasts with no `profileUUID` at all are excluded, as are empty ones. Omitting the parameter, or passing it empty, returns everything.

Both sides are canonicalized before comparing, using the same `_normalize_profile_uuid` the import path uses: a dashed, lowercase or uppercase uuid matches its canonical `UUIDUtils.generateUUID()` form, on either the document or the query string. That matters because `FirestoreRoast.profileUUID` is whatever the writing app version held. Run `/roastlog/diagnose` to see the real values in an account — it reports `distinctProfileUUIDs`, `roastsWithoutProfileUUID` and how many already match the canonical form.

**This filter is client-side, exactly like `lastEditedMillis`, and does not reduce Firestore reads.** `_fetch_collection` still pages the whole collection before the comparison happens; only the response shrinks. Firestore's `documents:list` cannot filter on a field, and `:runQuery` is not usable on this layout (see the `dateLastEdited` note above). So filter early with `lastEditedMillis` if the account is large — the two compose.

### `parseRoastData` cost

`roastPoints` is **additive**: `roastData` (the raw CSV) is returned either way. A ~700-point roast is 700×16 dict entries, so `parseRoastData=true` on 50 roasts is roughly **4.4× the bytes** of the default response. Combine it with `lastEditedMillis` so a delta sync only decodes the roasts that actually changed.

`omitRawRoastData=true` then halves that again, because the points already carry everything the CSV holds. It is **opt-in** on purpose: `roastData` is the field `POST /roastlog/import` writes back verbatim, so returning it empty would silently store an empty roast curve on a `GET` → `POST` round-trip. Use it only for clients that read the points and never re-post the body.

The endpoint serializes the response itself (`model_dump_json`) rather than letting FastAPI walk it from `response_model`. FastAPI's `jsonable_encoder` recurses into every leaf even though the values are already plain `float`/`str`/`None`; Pydantic's Rust serializer does the same JSON ~5× faster with compact separators. OpenAPI is unchanged — `response_model` still documents the schema.

### GET /roastlog/{roastID}

```bash
curl "http://localhost:8000/roastlog/9CEF1A2B3C4D5E6F708192A3B4C5D6E?parseRoastData=true" \
  -H "Authorization: Bearer <idToken>"
```

Reads one roast log by document id, with its `roastNoteLogs` document joined onto `notes`. Accepts `parseRoastData` and `omitRawRoastData`; the body is a single object, not an array.

It reads the document directly via `documents:get` instead of listing the collection, so it costs **two round trips** (roast + notes) whatever the account size — `GET /roastlog` has to page the whole collection and filter client-side.

- **404** when the id does not exist. Documents are namespaced per uid, so another account's roast id is simply not found.
- **400** when the id is not a usable document id (empty, `.`, `..`, or containing `/`, `\` or control characters) — the same guard `POST /roastlog/import` uses, so reads and writes accept exactly the same ids.
- `lastEditedMillis` and `onlyRoastsWithRoasterID` deliberately do **not** apply: the caller named the roast, so reusing those filters would only add a way for an existing roast to become a 404.

Declared after `/roastlog/diagnose` on purpose — FastAPI matches routes in declaration order, so the literal `/roastlog/diagnose` has to keep winning over the `{roastID}` pattern.

Note that `notes.roastID` comes back empty on this route, exactly as it does on `GET /roastlog`: the join keys off the roast document's id and never writes one back into the notes payload.

### POST /roastlog/import

```bash
curl -X POST http://localhost:8000/roastlog/import \
  -H "Authorization: Bearer <idToken>" \
  -H "Content-Type: application/json" \
  -d '{
    "roastID": "9CEF1A2B3C4D5E6F708192A3B4C5D6E",
    "profileUUID": "90A99999888777666555443332221100",
    "roastDate": "2024-05-01T10:20:30Z",
    "roasterID": "3",
    "roastSettings": "1,100,2,50,3,0.85",
    "roastData": "time,fan set,setpoint,...\n5.5,30.0,100.0,1200,120.25,ROASTING,...",
    "notes": {"coffeeType": "Ethiopia", "dose": 250, "yield": 212.4,
              "firstCrackMode": "AUTO", "firstCrackTime": 118.5, "firstCrackTemp": 196.3}
  }'
```

The request body has the same shape as a `GET /roastlog` entry, so a log can be moved from one account to another by fetching and re-posting it unchanged.

| Field | Type | Description |
|---|---|---|
| `roastID` | str | Firestore document id. Generated (32 uppercase hex, like `UUIDUtils.generateUUID()`) when omitted. Must not contain `/` |
| `profileUUID` | str | Profile this roast ran. Canonicalised to the app's uppercase dash-less form when it is a UUID; any other value is kept as-is |
| `roastDate` | str | RFC3339, or epoch milliseconds |
| `dateLastEdited` | str | Same formats. Defaults to now |
| `roasterID` | str | **string**. The current app writes the small roaster id (`PRO100=1`, `PRO200=2`, `PRO300=3`, `PRO500=4`, `PRO1000=5`); older documents carry a machine identifier or nothing. Pass it through as read |
| `roaster_config_code` | int | Roaster feature bitmask, default `0`. Sent as `roaster_config_code`, the name the current app reads (older documents in the wild use `roasterConfig`) |
| `deviceID`, `roastSettings`, `roastData` | str | Stored verbatim |
| `userID` | str | Accepted so a `GET` body round-trips, but **always** replaced by the authenticated uid on write |
| `notes` | object | Optional; written to `roastNoteLogs/{roastID}` |
| `generateRoastID` | bool | Force a fresh `roastID` instead of overwriting an existing document |

Writing matches the app: a full document set on `roastDetailedLog/{roastID}`, plus `roastNoteLogs/{roastID}` when `notes` is supplied — both in one atomic commit, like `RoastLogsRepository.synchronizeRoastLogToRemote`.

Notes fields: `roastID`, `coffeeID`, `coffeeType`, `colorChangeTemp`, `colorChangeTime`, `country`, `dateLastEdited`, `dose`, `firstCrackMode` (default `MANUAL`), `firstCrackTemp`, `firstCrackTime`, `isArchived`, `notes`, `numberOfRoasts`, `photo`, `timeZoneOffset`, and `yield_` — spelled `yield_` in the API because `yield` is a JavaScript reserved word that breaks Swagger UI's schema compiler, but read from and written to Firestore as `yield`. Legacy documents also carry `dtr`, `endTemp`, `extraInfo`, `preHeatPower`, `preHeatTemp`, `roastingTime`, `secondCrackTemp`, `secondCrackTime`, `creatorID`, `appDeviceID` and a `roastDetailedLog` reference; these are read and exposed but not written by an import. Every `calculated*` field and `archived` are `@Exclude` in the app and are therefore neither written nor read.

### POST /roastlog/import/bulk

```bash
curl -X POST http://localhost:8000/roastlog/import/bulk \
  -H "Authorization: Bearer <idToken>" \
  -H "Content-Type: application/json" \
  -d '{
    "roasts": [
      {"roastID": "9CEF...", "roastData": "time,fan set,...", "roasterID": "3"},
      {"roastID": "90A9...", "roastData": "time,fan set,...", "roasterID": "3"}
    ],
    "maxConcurrency": 4,
    "skipDuplicates": true
  }'
```

One invalid roast does not fail the batch: every item gets its own status.

| Field | Type | Default | Description |
|---|---|---|---|
| `roasts` | list | required | 1 to 500 entries, same objects as `POST /roastlog/import` |
| `maxConcurrency` | int | 4 | Parallel commits, between 1 and 16 |
| `skipDuplicates` | bool | true | Skip entries whose `roastID` already appears in the batch |

```json
{
  "total": 3, "imported": 2, "failed": 1, "duplicates": 0,
  "results": [
    {"index": 0, "status": "imported", "roastID": "9CEF...", "profileUUID": "90A9...", "roastDate": "2024-05-01T10:20:30Z", "error": null},
    {"index": 1, "status": "imported", "roastID": "90A9...", "profileUUID": null, "roastDate": null, "error": null},
    {"index": 2, "status": "invalid_roast", "roastID": null, "profileUUID": "p", "roastDate": null, "error": "invalid roastID: 'bad/id'"}
  ]
}
```

Per-item statuses: `imported`, `invalid_roast` (invalid `roastID`), `duplicate_in_batch`, `firestore_error`. Writes go out through Firestore `:commit` in chunks of 500 (the batch write limit); a chunk is only ever split at a roast boundary, so a roast and its notes always land together, and a failed chunk reports every roast it contained as failed.

### `roastData` (the CSV)

`roastData` is the curve exactly as the app stores it: `Roast.getRoastLog()` = one header line plus one line per `RoasterStatus.toCsvString()`. So the file the app attaches to its "share roast" email can be pasted straight into `roastData`.

Header (`RoastCsvHeader.getHeadersListInOrderAsCsv()`), with the value each column carries:

```
time,fan set,setpoint,fan speed,temp above,state,heater,p,i,d,temp below,temp board,j,ror_above,abs_humidity,abs_humidity_roc
```

| Column | Field | Unit |
|---|---|---|
| `time` | `time` | s |
| `fan set` | `fanSet` | % |
| `setpoint` | `setPoint` | °C |
| `fan speed` | `fanRpmSet` | rpm |
| `temp above` | `tempAbove` | °C |
| `state` | `MachState.name()` | e.g. `ROASTING` — a name, not a number |
| `heater` | `heater` | 0–1 |
| `p`, `i`, `d` | `p`, `i`, `d` | PID terms |
| `temp below` | `tempBelowFiltered` | °C |
| `temp board` | `boardTemp` | °C |
| `j` | `j` | term |
| `ror_above` | `rorAbove` | °C/min |
| `abs_humidity` | `absoluteHumidity` | % |
| `abs_humidity_roc` | `humidityRateOfChange` | %/min |

`roastSettings` is `RoasterSettings.serialize()`: comma-separated `id,value` pairs, e.g. `1,100,2,50,3,0.85`. The app can show it symbolically with `serializeHuman()` (`RoasterSettingId.name()`).

Pass `parseRoastData=true` to `GET /roastlog` to get `roastData` decoded into `roastPoints`, using the field names above as keys.

## Profile format

Profiles carry their temperature curve (`roastTempPoints`) and fan curve (`roastFanPoints`) encoded as comma-separated strings. To decode them, refer to the protobuf format of the [ikawa-share-url-decode](https://github.com/melchior-voidwolf/ikawa-share-url-decode) project.

### `shareUrl` field

Every profile is returned with a `shareUrl` field reproducing exactly the share link generated by the IKAWA app (`ShareProfileUtil.getProfileShareUrl`):

```
https://share.ikawa.support/profile/?<base64>
```

The `uuid` field is encoded as 16 raw bytes, so sentinel documents that are not real roast profiles (the account contains `EXTERNALROAST` and `externalRoast` placeholders named "External Roast") cannot carry a share URL. They are still listed, with `shareUrl` set to `null`.

The base64 encodes a Wire protobuf `RoastProfile` (same as `Profile.convertToProtobuf` in the app):

| Field | Type | Content |
|---|---|---|
| 1 `schema` | int32 | `schemeVersion` |
| 2 `id` | bytes | profile UUID (16 raw bytes) |
| 3 `name` | string | profile name |
| 4 `tempPoints` | repeated | `{1: time (ds), 2: temp (0.1 °C)}` |
| 5 `fanPoints` | repeated | `{1: time (ds), 2: power (0-255)}` |
| 6 `tempSensor` | enum | `ABOVE_BEANS=0`, `BELOW_BEANS=1` (`ABOVE_ROBUST` is mapped to `ABOVE_BEANS`) |
| 7 `cooldownFan` | message | `{1: time (ds), 2: power (0-255)}` |

Details faithful to the app:
- the **first point** of each curve keeps its value but has its **time forced to 0**;
- fan values are converted from % to 0-255 (`power/100*255`, integer truncation like the Kotlin `(int)` cast);
- time and temperature are multiplied by 10.

Decoding example (using the ikawa-share-url-decode project):

```bash
node index.js "$(curl -s http://localhost:8000/profiles -H 'Authorization: Bearer <idToken>' | jq -r '.[0].shareUrl')"
```
