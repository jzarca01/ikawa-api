import base64
import csv
import io
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

load_dotenv()

PORT = int(os.getenv("PORT", 8000))
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID")

ID_TOKEN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents"

# Share URL format (ShareProfileUtil.java: getProfileShareUrl + R.string.ikawa_share_url)
# base + Base64(NO_WRAP) of the Wire protobuf RoastProfile (roasterlibrary)
SHARE_URL_PREFIX = "https://share.ikawa.support/profile/?"


class LoginRequest(BaseModel):
    email: str
    password: str


class ImportRequest(BaseModel):
    shareUrl: str


class BulkImportRequest(BaseModel):
    shareUrls: list[str] = Field(min_length=1, max_length=500)
    maxConcurrency: int = Field(default=4, ge=1, le=16)
    skipDuplicates: bool = True

    @field_validator("shareUrls", mode="before")
    @classmethod
    def _split_string_input(cls, v):
        # practical tolerance: also accept a single string (newline/comma/space separated)
        if isinstance(v, str):
            return [item for item in re.split(r"[,\s]+", v.strip()) if item]
        return v


class BulkImportItemResult(BaseModel):
    index: int
    status: str  # imported | invalid_share_url | duplicate_in_batch | firestore_error
    uuid: Optional[str] = None
    name: Optional[str] = None
    error: Optional[str] = None


class BulkImportResponse(BaseModel):
    total: int
    imported: int
    failed: int
    duplicates: int
    results: list[BulkImportItemResult]


class LoginResponse(BaseModel):
    idToken: str
    refreshToken: str
    expiresIn: str
    localId: str


class ProfileResponse(BaseModel):
    uuid: str
    userID: str
    name: str
    coffeeName: Optional[str] = None
    coffeeID: Optional[str] = None
    shareUrl: Optional[str] = None
    roastTempPoints: Optional[str] = None
    roastFanPoints: Optional[str] = None
    isArchived: bool = False
    isFavourite: bool = False
    showInLibrary: bool = True
    schemeVersion: int = 1
    revision: int = 0
    tempSensor: int = 0
    cooldownPointTime: float = 0.0
    cooldownPointPower: float = 0.0
    dateCreated: Optional[str] = None
    dateLastEdited: Optional[str] = None
    dateLastRoasted: Optional[str] = None
    syncedTimestamp: Optional[str] = None


def firebase_login(email: str, password: str) -> LoginResponse:
    if not FIREBASE_API_KEY:
        raise HTTPException(500, "FIREBASE_API_KEY is not configured")

    resp = requests.post(
        f"{ID_TOKEN_URL}?key={FIREBASE_API_KEY}",
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200:
        raise HTTPException(401, data.get("error", {}).get("message", "Authentication failed"))

    return LoginResponse(**data)


def get_uid(id_token: str) -> str:
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))["user_id"]
    except Exception:
        raise HTTPException(401, "Invalid id token")


# ---------------------------------------------------------------- share URL decode
# Inverse of build_share_url: reproduces RoasterHelper.decodeBase64toProfile +
# Profile.convertFromProtobuf + IkawaProfile.fromLibraryProfile


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _parse_point(data: bytes) -> tuple[int, int]:
    time, power = 0, 0
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire != 0:
            raise ValueError("unexpected wire type in point")
        value, pos = _read_varint(data, pos)
        if field == 1:
            time = value
        elif field == 2:
            power = value
    return time, power


def _decode_base64_payload(share_url: str) -> bytes:
    # app's substringAfter(uri, "?", "")
    b64 = share_url.split("?", 1)[1] if "?" in share_url else share_url
    b64 = b64.replace("_", "/").replace("-", "+")
    for candidate in (b64, b64 + "=" * (-len(b64) % 4)):
        try:
            return base64.b64decode(candidate)
        except Exception:
            continue
    raise ValueError("invalid base64 payload in share URL")


def decode_share_url(share_url: str) -> dict:
    """Decodes an IKAWA share URL into a profile (handles standard/url-safe base64 and stripped padding)."""
    try:
        data = _decode_base64_payload(share_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    profile: dict = {
        "schema": 0,
        "uuid": "",
        "name": "",
        "tempPoints": [],
        "fanPoints": [],
        "tempSensor": 0,
        "cooldown": (0, 0),
    }
    pos = 0
    try:
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            field, wire = tag >> 3, tag & 7
            if wire == 0:
                value, pos = _read_varint(data, pos)
                if field == 1:
                    profile["schema"] = value
                elif field == 6:
                    profile["tempSensor"] = value
            elif wire == 2:
                length, pos = _read_varint(data, pos)
                payload = data[pos : pos + length]
                pos += length
                if field == 2:
                    profile["uuid"] = payload.hex().upper()
                elif field == 3:
                    profile["name"] = payload.decode("utf-8", errors="replace")
                elif field == 4:
                    profile["tempPoints"].append(_parse_point(payload))
                elif field == 5:
                    profile["fanPoints"].append(_parse_point(payload))
                elif field == 7:
                    profile["cooldown"] = _parse_point(payload)
    except Exception:
        raise HTTPException(400, "malformed protobuf payload in share URL")

    if not profile["uuid"]:
        raise HTTPException(400, "share URL contains no profile id")
    return profile


def profile_from_share_url(share_url: str) -> ProfileResponse:
    """Converts the decoded protobuf into Firestore profile fields (fromLibraryProfile)."""
    p = decode_share_url(share_url)
    temp_points = ",".join(f"{t / 10},{v / 10}" for t, v in p["tempPoints"])
    fan_points = ",".join(f"{t / 10},{w / 255 * 100}" for t, w in p["fanPoints"])
    return ProfileResponse(
        uuid=p["uuid"],
        userID="",
        name=p["name"],
        coffeeName=None,
        coffeeID=None,
        roastTempPoints=temp_points,
        roastFanPoints=fan_points,
        isArchived=False,
        isFavourite=False,
        showInLibrary=True,
        schemeVersion=p["schema"] or 1,
        revision=0,
        tempSensor=1 if p["tempSensor"] == 1 else 0,
        cooldownPointTime=p["cooldown"][0] / 10,
        cooldownPointPower=p["cooldown"][1] / 255 * 100,
    )


def _write_profile_document(id_token: str, uid: str, profile: ProfileResponse, now: str) -> dict:
    fields = {
        "uuid": {"stringValue": profile.uuid},
        "name": {"stringValue": profile.name},
        "userID": {"stringValue": uid},
        "roastTempPoints": {"stringValue": profile.roastTempPoints},
        "roastFanPoints": {"stringValue": profile.roastFanPoints},
        "schemeVersion": {"integerValue": str(profile.schemeVersion)},
        "tempSensor": {"integerValue": str(profile.tempSensor)},
        "cooldownPointTime": {"doubleValue": profile.cooldownPointTime},
        "cooldownPointPower": {"doubleValue": profile.cooldownPointPower},
        "profileType": {"integerValue": "0"},
        "revision": {"integerValue": "0"},
        "isArchived": {"booleanValue": False},
        "isFavourite": {"booleanValue": False},
        "showInLibrary": {"booleanValue": True},
        "dateCreated": {"timestampValue": now},
        "dateLastEdited": {"timestampValue": now},
        "dateLastRoasted": {"timestampValue": "1970-01-01T00:00:00Z"},
        "syncedTimestamp": {"timestampValue": now},
    }
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    url = f"{FIRESTORE_BASE}/apps/pro/users/{uid}/roastProfiles/{profile.uuid}?{mask}"

    resp = requests.patch(
        url,
        headers={"Authorization": f"Bearer {id_token}"},
        json={"fields": fields},
        timeout=30,
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)

    profile.userID = uid
    profile.shareUrl = build_share_url(profile)
    return {
        "status": "imported",
        "document": f"apps/pro/users/{uid}/roastProfiles/{profile.uuid}",
        "profile": profile,
    }


def import_profile(id_token: str, share_url: str) -> dict:
    """Imports a profile decoded from a share URL into Firestore for the user.

    Reproduces FirestoreProfilesImpl.updateProfileAsync:
    document(uuid).set(firestoreProfile, SetOptions.merge()) at
    apps/pro/users/{uid}/roastProfiles/{uuid}.
    """
    uid = get_uid(id_token)
    profile = profile_from_share_url(share_url)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return _write_profile_document(id_token, uid, profile, now)


def import_profiles_bulk(
    id_token: str,
    share_urls: list[str],
    max_concurrency: int = 4,
    skip_duplicates: bool = True,
) -> BulkImportResponse:
    """Imports a batch of share URLs. An invalid URL does not fail the whole batch.

    Same write as the single import (SetOptions.merge()), with bounded workers
    to stay within Firestore quotas and duplicate UUIDs skipped by default.
    """
    uid = get_uid(id_token)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    results: list[Optional[BulkImportItemResult]] = [None] * len(share_urls)
    seen_uuids: set[str] = set()
    lock = threading.Lock()

    def work(index: int, share_url: str) -> None:
        try:
            profile = profile_from_share_url(share_url)
        except Exception as e:
            detail = getattr(e, "detail", None) or str(e)
            results[index] = BulkImportItemResult(
                index=index, status="invalid_share_url", error=str(detail)[:500]
            )
            return

        if skip_duplicates:
            with lock:
                if profile.uuid in seen_uuids:
                    results[index] = BulkImportItemResult(
                        index=index,
                        status="duplicate_in_batch",
                        uuid=profile.uuid,
                        name=profile.name,
                    )
                    return
                seen_uuids.add(profile.uuid)

        try:
            _write_profile_document(id_token, uid, profile, now)
        except Exception as e:
            detail = getattr(e, "detail", None) or str(e)
            results[index] = BulkImportItemResult(
                index=index,
                status="firestore_error",
                uuid=profile.uuid,
                name=profile.name,
                error=str(detail)[:500],
            )
            return

        results[index] = BulkImportItemResult(
            index=index, status="imported", uuid=profile.uuid, name=profile.name
        )

    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        list(pool.map(lambda t: work(*t), enumerate(share_urls)))

    final = [r for r in results if r is not None]
    return BulkImportResponse(
        total=len(final),
        imported=sum(1 for r in final if r.status == "imported"),
        failed=sum(1 for r in final if r.status in ("invalid_share_url", "firestore_error")),
        duplicates=sum(1 for r in final if r.status == "duplicate_in_batch"),
        results=final,
    )


def fetch_profiles(id_token: str) -> list[ProfileResponse]:
    uid = get_uid(id_token)
    url = f"{FIRESTORE_BASE}/apps/pro/users/{uid}/roastProfiles"
    headers = {"Authorization": f"Bearer {id_token}"}

    documents = []
    page_token = None
    while True:
        params = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        code, detail = _firestore_error(resp)
        if code is not None or resp.status_code != 200:
            raise HTTPException(resp.status_code if resp.status_code != 200 else (code or 502),
                                detail or "Failed to fetch profiles")
        data = resp.json()
        documents.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    profiles = []
    for doc in documents:
        fields = _firestore_fields(doc)
        profile = ProfileResponse(
            uuid=doc.get("name", "").split("/")[-1],
            userID=_firestore_field(fields, "userID") or "",
            name=_firestore_field(fields, "name") or "",
            coffeeName=_firestore_field(fields, "coffeeName"),
            coffeeID=_firestore_field(fields, "coffeeID"),
            roastTempPoints=_firestore_field(fields, "roastTempPoints"),
            roastFanPoints=_firestore_field(fields, "roastFanPoints"),
            isArchived=bool(_firestore_field(fields, "isArchived") or False),
            isFavourite=bool(_firestore_field(fields, "isFavourite") or False),
            showInLibrary=bool(
                _firestore_field(fields, "showInLibrary")
                if _firestore_field(fields, "showInLibrary") is not None
                else True
            ),
            schemeVersion=_as_int(_firestore_field(fields, "schemeVersion"), 1),
            revision=_as_int(_firestore_field(fields, "revision")),
            tempSensor=_as_int(_firestore_field(fields, "tempSensor")),
            cooldownPointTime=_as_float(_firestore_field(fields, "cooldownPointTime")),
            cooldownPointPower=_as_float(_firestore_field(fields, "cooldownPointPower")),
            dateCreated=_firestore_field(fields, "dateCreated"),
            dateLastEdited=_firestore_field(fields, "dateLastEdited"),
            dateLastRoasted=_firestore_field(fields, "dateLastRoasted"),
            syncedTimestamp=_firestore_field(fields, "syncedTimestamp"),
        )
        profiles.append(profile)

    for profile in profiles:
        try:
            profile.shareUrl = build_share_url(profile)
        except Exception:
            # one unencodable profile must not fail the whole listing
            profile.shareUrl = None

    return profiles


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _int_field(field: int, value: int) -> bytes:
    tag = (field << 3) | 0
    return _varint(tag) + _varint(value)


def _bytes_field(field: int, value: bytes) -> bytes:
    tag = (field << 3) | 2
    return _varint(tag) + _varint(len(value)) + value


def _parse_point_pairs(raw: Optional[str]) -> list[tuple[float, float]]:
    if not raw:
        return []
    values = raw.split(",")
    points = []
    for i in range(0, len(values) - 1, 2):
        try:
            points.append((float(values[i]), float(values[i + 1])))
        except ValueError:
            # tolerate a malformed trailing pair rather than failing the profile
            break
    return points


def build_share_url(profile: ProfileResponse) -> Optional[str]:
    """Reproduces getProfileShareUrl() from ShareProfileUtil (IKAWA app).

    Wire RoastProfile proto: {1: schema, 2: id (uuid raw bytes), 3: name,
    4: tempPoints {1: time (ds), 2: temp (0.1 °C)}, 5: fanPoints {1: time (ds), 2: power (0-255)},
    6: tempSensor, 7: cooldownFan}. The first point of each curve keeps its value
    but has its time forced to 0 (Profile.convertToProtobuf).

    Returns None when the profile id cannot be encoded as the 16 raw bytes of
    field 2. The account holds sentinel documents (uuid "EXTERNALROAST" /
    "externalRoast") that are not real roast profiles; they have no share URL.
    """
    # TempSensor: ABOVE_BEANS=0, BELOW_BEANS=1, ABOVE_ROBUST=2 -> mapped to ABOVE_BEANS
    temp_sensor = 1 if profile.tempSensor == 1 else 0

    try:
        uuid_bytes = bytes.fromhex(profile.uuid.replace("-", "").replace(" ", ""))
    except (ValueError, AttributeError):
        return None
    if len(uuid_bytes) != 16:
        return None

    temp_points = _parse_point_pairs(profile.roastTempPoints)
    fan_points = _parse_point_pairs(profile.roastFanPoints)

    msg = bytearray()
    msg += _int_field(1, profile.schemeVersion)
    msg += _bytes_field(2, uuid_bytes)
    msg += _bytes_field(3, profile.name.encode())
    for i, (time_s, temp_c) in enumerate(temp_points):
        t = 0 if i == 0 else int(time_s * 10)
        msg += _bytes_field(4, _int_field(1, t) + _int_field(2, int(temp_c * 10)))
    for i, (time_s, power_pct) in enumerate(fan_points):
        t = 0 if i == 0 else int(time_s * 10)
        msg += _bytes_field(5, _int_field(1, t) + _int_field(2, int(power_pct / 100 * 255)))
    msg += _int_field(6, temp_sensor)
    msg += _bytes_field(
        7,
        _int_field(1, int(profile.cooldownPointTime * 10))
        + _int_field(2, int(profile.cooldownPointPower / 100 * 255)),
    )

    b64 = base64.b64encode(bytes(msg)).decode()
    return SHARE_URL_PREFIX + b64


# ---------------------------------------------------------------- roast log
# Roast logs live in their own Firestore collections, not alongside profiles
# (FirestorePaths.kt: collectionUserRoastLogs / collectionUserRoastNotes):
#
#   apps/pro/users/{uid}/roastDetailedLog/{roastID}   -> FirestoreRoast
#   apps/pro/users/{uid}/roastNoteLogs/{roastID}      -> FirestoreRoastNotes
#
# Read (FirestoreRoastLogsImpl.getUserRoastLogs): collection query filtered on
# dateLastEdited >= Timestamp(lastEditedMillisFilter), mapped to FirestoreRoast,
# then the documents whose roasterID does not parse as an int are dropped
# (FirestoreRoast.getRoasterID() returns -1).
#
# Write (FirestoreRoastLogsImpl.updateRoast): document(roastID).set(roast) --
# a full overwrite, no merge. RoastLogsRepository.synchronizeRoastLogToRemote
# writes the roast and its notes together.

ROAST_LOG_COLLECTION = "roastDetailedLog"
ROAST_NOTES_COLLECTION = "roastNoteLogs"
FIRESTORE_COMMIT_URL = f"{FIRESTORE_BASE}:commit"
FIRESTORE_MAX_BATCH_WRITES = 500

# RoastCsvHeader.getHeadersListInOrderAsCsv() -- the header line of `roastData`,
# which is itself Roast.getRoastLog() = that header + one CSV line per
# RoasterStatus.toCsvString().
ROAST_CSV_HEADER = (
    "time,fan set,setpoint,fan speed,temp above,state,heater,p,i,d,"
    "temp below,temp board,j,ror_above,abs_humidity,abs_humidity_roc"
)
# RoasterStatus.toCsvString() writes these values in RoastCsvHeader order.
ROAST_CSV_KEYS = (
    "time",
    "fanSet",
    "setPoint",
    "fanSpeed",
    "tempAbove",
    "state",
    "heater",
    "p",
    "i",
    "d",
    "tempBelow",
    "boardTemp",
    "j",
    "rorAbove",
    "absoluteHumidity",
    "absoluteHumidityRateOfChange",
)


class RoastNotesResponse(BaseModel):
    # FirestoreRoastNotes.toFirestore() writes the first block; the rest are the
    # fields real documents carry (they are @IgnoreExtraProperties on the app
    # side, so it reads them without them being part of its own model). Every
    # calculated* field and `archived` are @Exclude and are never stored.
    roastID: str
    coffeeID: Optional[str] = None
    coffeeType: Optional[str] = None
    colorChangeTemp: float = 0.0
    colorChangeTime: float = 0.0
    country: Optional[str] = None
    dateLastEdited: Optional[str] = None
    dose: float = 0.0
    firstCrackMode: str = "MANUAL"
    firstCrackTemp: float = 0.0
    firstCrackTime: float = 0.0
    isArchived: bool = False
    notes: str = ""
    numberOfRoasts: int = 0
    photo: Optional[str] = None
    # Stored as a double in real documents even though the app models it as Int.
    timeZoneOffset: float = 0.0
    # `yield` is a Python keyword, and the Firestore field name is also a
    # JavaScript reserved word: Swagger UI compiles schema models into JS and
    # fails with "Could not render" on such a property. The API therefore
    # exposes it as yield_; the Firestore field is still read/written as "yield".
    yield_: float = 0.0
    # Legacy / extra fields present on real roastNoteLogs documents.
    dtr: float = 0.0
    endTemp: float = 0.0
    extraInfo: str = ""
    preHeatPower: float = 0.0
    preHeatTemp: float = 0.0
    roastingTime: float = 0.0
    secondCrackTemp: float = 0.0
    secondCrackTime: float = 0.0
    appDeviceID: Optional[str] = None
    bluetoothID: Optional[str] = None
    creatorID: Optional[str] = None
    wasUploadedToFirebaseEver: Optional[bool] = None
    roastDetailedLog: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class RoastLogResponse(BaseModel):
    # FirestoreRoast as written by the current app: roaster_config_code and
    # userID exist there. Real accounts also hold older documents written before
    # that schema, which use `roasterConfig` and carry extra legacy fields and no
    # userID. Both shapes are read; roaster_config_code is filled from whichever
    # is present so callers do not have to care which app version wrote the log.
    # `syncedTimestamp` is @Exclude and is never stored.
    roastID: str
    userID: str = ""
    deviceID: Optional[str] = None
    profileUUID: str = ""
    roastDate: Optional[str] = None
    dateLastEdited: Optional[str] = None
    # Observed in real accounts: the current app writes a small roaster id as a
    # string ("3"), older documents carry a machine identifier ("580152") and
    # the oldest ones leave it null. All three are passed through untouched.
    roasterID: Optional[str] = None
    roaster_config_code: int = 0
    roastSettings: str = ""
    roastData: str = ""
    roastPoints: Optional[list[dict[str, Any]]] = None
    notes: Optional[RoastNotesResponse] = None
    # Legacy fields found on older documents; surfaced so nothing is hidden.
    roasterConfig: Optional[int] = None
    machineUUID: Optional[str] = None
    bluetoothID: Optional[str] = None
    csvOnServer: Optional[bool] = None
    dataOnServer: Optional[bool] = None
    wasUploadedToFirebaseEver: Optional[bool] = None


class RoastLogImportRequest(BaseModel):
    """Single roast import.

    Same shape as RoastLogResponse, so a GET /roastlog body can be re-posted
    as-is (account A -> account B). `roastData` is the raw CSV exactly as the app
    stores it, which is also what the app attaches to its "share roast" email
    ({roastID}.csv), so a CSV obtained that way plus the metadata fields below
    is a valid payload too.

    `roastID` is generated (UUIDUtils.generateUUID(): uppercase, dashes removed)
    when omitted.
    """

    roastID: Optional[str] = None
    profileUUID: str = ""
    roastDate: Optional[str] = None
    dateLastEdited: Optional[str] = None
    roasterID: str = ""
    roaster_config_code: int = 0
    deviceID: str = ""
    roastSettings: str = ""
    roastData: str = ""
    userID: Optional[str] = None
    notes: Optional[RoastNotesResponse] = None
    # Generates a fresh roastID instead of reusing an existing document.
    generateRoastID: bool = False

    # `userID` is accepted so a GET /roastlog body round-trips unchanged, but it
    # is always replaced by the authenticated uid on write.


class RoastLogBulkImportRequest(BaseModel):
    roasts: list[RoastLogImportRequest] = Field(min_length=1, max_length=500)
    maxConcurrency: int = Field(default=4, ge=1, le=16)
    skipDuplicates: bool = True


class RoastLogBulkItemResult(BaseModel):
    index: int
    status: str  # imported | invalid_roast | duplicate_in_batch | firestore_error
    roastID: Optional[str] = None
    profileUUID: Optional[str] = None
    roastDate: Optional[str] = None
    error: Optional[str] = None


class RoastLogBulkImportResponse(BaseModel):
    total: int
    imported: int
    failed: int
    duplicates: int
    results: list[RoastLogBulkItemResult]


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_timestamp(value: str | int | float | datetime | None) -> str:
    """Normalizes a date to the RFC3339 form Firestore expects/returns.

    Accepts a datetime, epoch milliseconds (the unit the app uses for
    lastEditedMillisFilter via new Date(millis)), a numeric string holding epoch
    milliseconds, or an already-RFC3339 string, which is what Firestore hands
    back on reads.
    """
    if value is None:
        return "1970-01-01T00:00:00Z"
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        return (
            datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    text = str(value).strip()
    if not text:
        return "1970-01-01T00:00:00Z"
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return _to_timestamp(float(text))
    return text  # already RFC3339; let Firestore validate it


def _firestore_string(value: str) -> dict:
    return {"stringValue": value}


def _firestore_timestamp(value: str | int | float | datetime | None) -> dict:
    return {"timestampValue": _to_timestamp(value)}


def _firestore_double(value: float) -> dict:
    return {"doubleValue": float(value)}


def _firestore_int(value: int) -> dict:
    return {"integerValue": str(int(value))}


def _firestore_bool(value: bool) -> dict:
    return {"booleanValue": bool(value)}


def _firestore_null() -> dict:
    return {"nullValue": None}


def _parse_roast_points(roast_data: str) -> list[dict[str, Any]]:
    """Parses `roastData` CSV into typed points (RoastEntry-ish).

    The header line is dropped; every other line is split on commas. Unknown or
    empty columns become None rather than raising, so a partially-written CSV
    still yields usable points.
    """
    if not roast_data:
        return []
    reader = csv.reader(io.StringIO(roast_data))
    try:
        header = next(reader)
    except StopIteration:
        return []
    if [h.strip() for h in header] == ROAST_CSV_HEADER.split(","):
        keys = ROAST_CSV_KEYS
    else:
        keys = [h.strip() for h in header] or ROAST_CSV_KEYS

    # Each row is stripped once and the row is padded/truncated to len(keys)
    # before the zip. The previous shape stripped every cell twice -- once for
    # the all-empty-row test, once for the value -- and re-checked `i < len(row)`
    # per cell, which on a 700-point roast meant ~12 600 strip() calls.
    nkeys = len(keys)
    points: list[dict[str, Any]] = []
    for row in reader:
        cells = [cell.strip() for cell in row]
        if not any(cells):
            continue
        if len(cells) < nkeys:
            cells += [""] * (nkeys - len(cells))
        elif len(cells) > nkeys:
            del cells[nkeys:]
        point: dict[str, Any] = {}
        for key, raw in zip(keys, cells):
            if not raw:
                point[key] = None
                continue
            try:
                point[key] = float(raw)
            except ValueError:
                point[key] = raw  # `state` is a MachState name, e.g. ROASTING
        points.append(point)
    return points


_DOC_ID_INVALID = re.compile(r"[/\\\x00-\x1f\x7f]")
_UUID_HEX = re.compile(r"^[0-9A-F]{32}$")


def _generate_roast_id() -> str:
    return uuid.uuid4().hex.upper()


def _validate_document_id(doc_id: str) -> None:
    if not doc_id or doc_id in (".", "..") or _DOC_ID_INVALID.search(doc_id):
        raise HTTPException(400, f"invalid roastID: {doc_id!r}")


def _roast_document_fields(
    uid: str, roast: RoastLogImportRequest, roast_id: str, profile_uuid: str
) -> dict:
    """Builds the Firestore field map for a roastDetailedLog document.

    Mirrors FirestoreRoast + FirestoreRoastKt.toFirestore: userID is always the
    authenticated uid (never the one carried by a GET body, so importing a log
    exported from another account stamps the importer), and syncedTimestamp is
    left out because it is @Exclude on FirestoreRoast.
    """
    date_last_edited = roast.dateLastEdited or _now_rfc3339()
    return {
        "roastID": _firestore_string(roast_id),
        "userID": _firestore_string(uid),
        "deviceID": _firestore_string(roast.deviceID),
        "profileUUID": _firestore_string(profile_uuid),
        "roastDate": _firestore_timestamp(roast.roastDate),
        "dateLastEdited": _firestore_timestamp(date_last_edited),
        "roasterID": _firestore_string(roast.roasterID),
        "roaster_config_code": _firestore_int(roast.roaster_config_code),
        "roastSettings": _firestore_string(roast.roastSettings),
        "roastData": _firestore_string(roast.roastData),
    }


def _roast_notes_document_fields(
    uid: str, notes: RoastNotesResponse, roast_id: str, date_last_edited: str
) -> dict:
    """Builds the Firestore field map for a roastNoteLogs document.

    Only the properties FirestoreRoastNotes.toFirestore() populates are written;
    the calculated* ones and `archived` are @Exclude in the app.
    `isArchived` is a public field on FirestoreRoastNotes (its getArchived() is
    @Exclude), so that is the name stored in Firestore.
    """
    return {
        "roastID": _firestore_string(roast_id),
        "coffeeID": _firestore_string(notes.coffeeID) if notes.coffeeID is not None else _firestore_null(),
        "coffeeType": _firestore_string(notes.coffeeType) if notes.coffeeType is not None else _firestore_null(),
        "colorChangeTemp": _firestore_double(notes.colorChangeTemp),
        "colorChangeTime": _firestore_double(notes.colorChangeTime),
        "country": _firestore_string(notes.country) if notes.country is not None else _firestore_null(),
        "dateLastEdited": _firestore_timestamp(notes.dateLastEdited or date_last_edited),
        "dose": _firestore_double(notes.dose),
        "firstCrackMode": _firestore_string(notes.firstCrackMode or "MANUAL"),
        "firstCrackTemp": _firestore_double(notes.firstCrackTemp),
        "firstCrackTime": _firestore_double(notes.firstCrackTime),
        "isArchived": _firestore_bool(notes.isArchived),
        "notes": _firestore_string(notes.notes),
        "numberOfRoasts": _firestore_int(notes.numberOfRoasts),
        "photo": _firestore_string(notes.photo) if notes.photo is not None else _firestore_null(),
        "timeZoneOffset": _firestore_int(notes.timeZoneOffset),
        "yield": _firestore_double(notes.yield_),
    }


def _normalize_profile_uuid(profile_uuid: Optional[str]) -> str:
    """Canonicalizes a profile uuid to the form the app stores it in.

    UUIDUtils.generateUUID() yields an uppercase uuid with the dashes removed,
    which is what FirestoreRoast.profileUUID holds. A value that is a uuid in
    any other shape is rewritten to that form so the roast still resolves its
    profile; anything else is left verbatim rather than rejected, because a roast
    log stays valid even when the referenced profile does not exist.
    """
    if not profile_uuid:
        return ""
    candidate = profile_uuid.strip().replace("-", "").upper()
    return candidate if _UUID_HEX.match(candidate) else profile_uuid.strip()


def _resolve_roast_id(roast: RoastLogImportRequest) -> str:
    if roast.generateRoastID or not roast.roastID:
        return _generate_roast_id()
    roast_id = roast.roastID.strip()
    _validate_document_id(roast_id)
    return roast_id


def _build_writes(uid: str, roast: RoastLogImportRequest) -> tuple[str, list[dict]]:
    """Returns (roastID, writes) for the roastDetailedLog + roastNoteLogs pair."""
    roast_id = _resolve_roast_id(roast)
    date_last_edited = roast.dateLastEdited or _now_rfc3339()
    profile_uuid = _normalize_profile_uuid(roast.profileUUID)
    writes = [
        {
            "update": {
                "name": f"apps/pro/users/{uid}/{ROAST_LOG_COLLECTION}/{roast_id}",
                "fields": _roast_document_fields(uid, roast, roast_id, profile_uuid),
            }
        }
    ]
    if roast.notes is not None:
        notes = roast.notes.model_copy(update={"roastID": roast_id})
        writes.append(
            {
                "update": {
                    "name": f"apps/pro/users/{uid}/{ROAST_NOTES_COLLECTION}/{roast_id}",
                    "fields": _roast_notes_document_fields(uid, notes, roast_id, date_last_edited),
                }
            }
        )
    return roast_id, writes


def _commit(id_token: str, writes: list[dict]) -> None:
    resp = requests.post(
        FIRESTORE_COMMIT_URL,
        headers={"Authorization": f"Bearer {id_token}"},
        json={"writes": writes},
        timeout=60,
    )
    code, detail = _firestore_error(resp)
    if code is not None:
        raise HTTPException(code, detail)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail or resp.text)


def import_roast(id_token: str, roast: RoastLogImportRequest) -> dict:
    """Imports one roast log.

    Reproduces FirestoreRoastLogsImpl.updateRoast + updateRoastNotes: a full
    document set (no merge) on roastDetailedLog/{roastID}, and on
    roastNoteLogs/{roastID} when notes are supplied.
    """
    uid = get_uid(id_token)
    roast_id, writes = _build_writes(uid, roast)
    _commit(id_token, writes)
    documents = [w["update"]["name"] for w in writes]
    return {"status": "imported", "roastID": roast_id, "documents": documents}


def import_roasts_bulk(
    id_token: str,
    roasts: list[RoastLogImportRequest],
    max_concurrency: int = 4,
    skip_duplicates: bool = True,
) -> RoastLogBulkImportResponse:
    """Imports a batch of roast logs.

    One invalid roast does not fail the batch: every item gets its own status.
    Writes go through Firestore :commit in chunks of 500 (the batch write limit),
    and each roast + its notes are written in the same atomic commit, like
    RoastLogsRepository.synchronizeRoastLogToRemote.
    """
    uid = get_uid(id_token)

    results: list[Optional[RoastLogBulkItemResult]] = [None] * len(roasts)
    prepared: list[tuple[int, RoastLogBulkItemResult, list[dict]]] = []
    seen_ids: set[str] = set()
    lock = threading.Lock()

    for index, roast in enumerate(roasts):
        try:
            roast_id, writes = _build_writes(uid, roast)
        except Exception as e:
            detail = getattr(e, "detail", None) or str(e)
            results[index] = RoastLogBulkItemResult(
                index=index,
                status="invalid_roast",
                profileUUID=roast.profileUUID or None,
                error=str(detail)[:500],
            )
            continue

        if skip_duplicates:
            with lock:
                if roast_id in seen_ids:
                    results[index] = RoastLogBulkItemResult(
                        index=index,
                        status="duplicate_in_batch",
                        roastID=roast_id,
                        profileUUID=roast.profileUUID or None,
                    )
                    continue
                seen_ids.add(roast_id)

        results[index] = RoastLogBulkItemResult(
            index=index,
            status="imported",
            roastID=roast_id,
            profileUUID=roast.profileUUID or None,
            roastDate=roast.roastDate,
        )
        prepared.append(
            (
                index,
                RoastLogBulkItemResult(index=index, status="imported", roastID=roast_id),
                writes,
            )
        )

    # Group every write of one roast into the same chunk whenever possible so a
    # roast and its notes stay atomic; chunks never exceed the 500 write limit.
    # A roast and its notes are only ever split across chunks at a roast
    # boundary, and each roast contributes at most 2 writes, so a chunk can never
    # exceed the 500-write batch limit.
    chunks: list[list[tuple[int, RoastLogBulkItemResult, list[dict]]]] = []
    current: list[tuple[int, RoastLogBulkItemResult, list[dict]]] = []
    used = 0
    limit = FIRESTORE_MAX_BATCH_WRITES
    for entry in prepared:
        size = len(entry[2])
        if current and used + size > limit:
            chunks.append(current)
            current, used = [], 0
        current.append(entry)
        used += size
    if current:
        chunks.append(current)

    def commit_chunk(chunk) -> None:
        writes = [w for _, _, ws in chunk for w in ws]
        try:
            _commit(id_token, writes)
        except Exception as e:
            detail = getattr(e, "detail", None) or str(e)
            # A failed commit rolls the whole chunk back, so every roast in it
            # is reported as failed.
            for index, item, _ in chunk:
                results[index] = item.model_copy(
                    update={"status": "firestore_error", "error": str(detail)[:500]}
                )

    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        list(pool.map(commit_chunk, chunks))

    final = [r for r in results if r is not None]
    return RoastLogBulkImportResponse(
        total=len(final),
        imported=sum(1 for r in final if r.status == "imported"),
        failed=sum(1 for r in final if r.status in ("invalid_roast", "firestore_error")),
        duplicates=sum(1 for r in final if r.status == "duplicate_in_batch"),
        results=final,
    )


def _firestore_error(resp_or_rows) -> tuple[Optional[int], str]:
    """Extracts (status code, message) from a Firestore REST response or stream.

    `:runQuery` is a streaming endpoint: its body is a JSON *array* even when the
    request fails, and a per-element error can arrive with HTTP 200. So the usual
    `{"error": {...}}` shape cannot be assumed, and the code has to come from the
    error object itself rather than from the HTTP status.
    """
    payload = resp_or_rows
    text = ""
    http_status: Optional[int] = None
    # An error that arrives with HTTP 200 (streamed, or a body that is not a
    # result array) must never be reported to the caller as success.
    fallback = 502
    if hasattr(resp_or_rows, "status_code"):
        http_status = resp_or_rows.status_code
        if http_status != 200:
            fallback = http_status
        text = getattr(resp_or_rows, "text", "") or ""
        try:
            payload = resp_or_rows.json()
        except ValueError:
            return fallback, text
    else:
        text = "[]"

    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.append(payload)
    elif isinstance(payload, list):
        candidates = [element for element in payload if isinstance(element, dict)]
    else:
        return fallback, text or f"unexpected body type {type(payload).__name__}"

    for candidate in candidates:
        error = candidate.get("error")
        if isinstance(error, dict) and error.get("message"):
            try:
                code = error.get("code")
                code = int(code) if code is not None else None
            except (TypeError, ValueError):
                code = None
            # Prefer the error's own code, then the non-2xx HTTP status, then 502.
            return code or fallback, str(error["message"])
    return None, text


def _as_int(value: Any, default: int = 0) -> int:
    """Coerces a Firestore value to int, falling back to `default`.

    Firestore types its own values, but a legacy or hand-edited document can
    still hold a string here, and one bad field must not abort a whole listing.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _firestore_reference(fields: Any, key: str) -> Optional[str]:
    """Reads a referenceValue (a document path) as a string."""
    if not isinstance(fields, dict):
        return None
    value = fields.get(key)
    if isinstance(value, dict):
        reference = value.get("referenceValue")
        if reference is not None:
            return str(reference)
    return None


def _opt_str(value: Any) -> Optional[str]:
    """None for an absent/null value, otherwise the value as a string."""
    return None if value is None else str(value)


def _opt_bool(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _firestore_fields(document: Any) -> dict:
    """Returns a document's field map, or {} when it is absent or not a map.

    Firestore's REST envelope is not guaranteed to be well-formed here: a
    streamed row can carry anything, so the shape is normalised once at the
    boundary instead of being assumed by every reader.
    """
    if not isinstance(document, dict):
        return {}
    fields = document.get("fields")
    return fields if isinstance(fields, dict) else {}


def _firestore_field(fields: Any, key: str) -> Any:
    """Reads one Firestore REST field value as a plain Python value.

    Returns None for a missing key, a non-map `fields`, or a field value that
    is not a typed `Value` (e.g. an array/map value), so one odd document cannot
    abort a whole listing.
    """
    if not isinstance(fields, dict):
        return None
    value = fields.get(key)
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "nullValue" in value:
        return None
    if "integerValue" in value:
        return _as_int(value["integerValue"])
    if "doubleValue" in value:
        return _as_float(value["doubleValue"])
    # arrayValue / mapValue / referenceValue: not a scalar this reader uses.
    # Returning the raw dict would leak into str()/float() callers, so report it
    # as absent instead.
    return None


def _roast_notes_from_fields(fields: dict) -> RoastNotesResponse:
    def num(key: str) -> float:
        return _as_float(_firestore_field(fields, key))

    def text(key: str) -> Optional[str]:
        value = _firestore_field(fields, key)
        return None if value is None else str(value)

    return RoastNotesResponse(
        roastID=str(_firestore_field(fields, "roastID") or ""),
        coffeeID=text("coffeeID"),
        coffeeType=text("coffeeType"),
        colorChangeTemp=num("colorChangeTemp"),
        colorChangeTime=num("colorChangeTime"),
        country=text("country"),
        dateLastEdited=text("dateLastEdited"),
        dose=num("dose"),
        firstCrackMode=str(_firestore_field(fields, "firstCrackMode") or "MANUAL"),
        firstCrackTemp=num("firstCrackTemp"),
        firstCrackTime=num("firstCrackTime"),
        isArchived=bool(_firestore_field(fields, "isArchived") or False),
        notes=str(_firestore_field(fields, "notes") or ""),
        numberOfRoasts=_as_int(_firestore_field(fields, "numberOfRoasts")),
        photo=text("photo"),
        # Stored as a double in real documents.
        timeZoneOffset=num("timeZoneOffset"),
        yield_=num("yield"),
        dtr=num("dtr"),
        endTemp=num("endTemp"),
        extraInfo=str(_firestore_field(fields, "extraInfo") or ""),
        preHeatPower=num("preHeatPower"),
        preHeatTemp=num("preHeatTemp"),
        roastingTime=num("roastingTime"),
        secondCrackTemp=num("secondCrackTemp"),
        secondCrackTime=num("secondCrackTime"),
        appDeviceID=text("appDeviceID"),
        bluetoothID=text("bluetoothID"),
        creatorID=text("creatorID"),
        wasUploadedToFirebaseEver=_opt_bool(_firestore_field(fields, "wasUploadedToFirebaseEver")),
        roastDetailedLog=_firestore_reference(fields, "roastDetailedLog"),
    )


def _roast_log_from_document(
    doc: dict, parse_roast_data: bool, omit_raw_roast_data: bool = False
) -> RoastLogResponse:
    fields = _firestore_fields(doc)
    roast_data = _firestore_field(fields, "roastData")
    roast_data = "" if roast_data is None else str(roast_data)
    device_id = _firestore_field(fields, "deviceID")
    roaster_config = _firestore_field(fields, "roasterConfig")
    return RoastLogResponse(
        roastID=str(doc.get("name", "")).split("/")[-1],
        userID=str(_firestore_field(fields, "userID") or ""),
        deviceID=None if device_id is None else str(device_id),
        profileUUID=str(_firestore_field(fields, "profileUUID") or ""),
        roastDate=_firestore_field(fields, "roastDate"),
        dateLastEdited=_firestore_field(fields, "dateLastEdited"),
        roasterID=_opt_str(_firestore_field(fields, "roasterID")),
        # Older documents store the config mask as `roasterConfig`; the current
        # app writes `roaster_config_code`. Prefer the current name.
        roaster_config_code=_as_int(
            _firestore_field(fields, "roaster_config_code")
            if _firestore_field(fields, "roaster_config_code") is not None
            else roaster_config
        ),
        roastSettings=str(_firestore_field(fields, "roastSettings") or ""),
        # `roastPoints` already carries everything `roastData` holds, so the
        # caller can drop the raw CSV to halve the response. It is opt-in
        # because `roastData` is the field POST /roastlog/import writes back
        # verbatim: returning it empty here would silently write an empty roast
        # curve on a naive GET -> POST round-trip.
        roastData="" if omit_raw_roast_data else roast_data,
        roastPoints=_parse_roast_points(roast_data) if parse_roast_data else None,
        roasterConfig=_as_int(roaster_config) if roaster_config is not None else None,
        machineUUID=_opt_str(_firestore_field(fields, "machineUUID")),
        bluetoothID=_opt_str(_firestore_field(fields, "bluetoothID")),
        csvOnServer=_opt_bool(_firestore_field(fields, "csvOnServer")),
        dataOnServer=_opt_bool(_firestore_field(fields, "dataOnServer")),
        wasUploadedToFirebaseEver=_opt_bool(_firestore_field(fields, "wasUploadedToFirebaseEver")),
    )


_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|z|[+-]\d{2}:?\d{2})?$"
)


def _timestamp_millis(value: Any) -> Optional[float]:
    """Parses an RFC3339 timestamp (or epoch millis) to epoch millis.

    Firestore emits up to nanosecond precision ("...T10:20:30.123456789Z") and
    datetime.fromisoformat only accepts 3 or 6 fractional digits before 3.11, so
    the value is matched and rebuilt here rather than handed to it directly.
    Returns None when the value cannot be read as a timestamp.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return float(text)
    match = _TIMESTAMP_RE.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, frac, tz = match.groups()
    micros = int((frac or "0")[:6].ljust(6, "0"))
    try:
        moment = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), micros,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    if tz and tz not in ("Z", "z"):
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = timedelta(minutes=sign * (int(digits[:2]) * 60 + int(digits[2:4])))
        moment -= offset
    return moment.timestamp() * 1000.0


def _fetch_collection(
    id_token: str, uid: str, collection: str, last_edited_millis: int = 0
) -> list[dict]:
    """Reads a user collection filtered on dateLastEdited >= lastEditedMillis.

    Uses documents:list, the same call the profile listing uses, so the
    collection is addressed the same proven way: parent path
    apps/pro/users/{uid} plus the collectionId segment. This is deliberately not
    :runQuery -- that method takes the document path as RunQueryRequest.parent,
    and the IKAWA layout (apps/pro/users/{uid}/{collection} is an odd, 5-segment
    path) is rejected there as a malformed document parent. documents:list also
    gives real pagination via pageSize/pageToken and nextPageToken.

    The dateLastEdited filter is therefore applied client-side instead of
    server-side. The result set matches the app's
    whereGreaterThanOrEqualTo("dateLastEdited", ...) exactly, including dropping
    documents that have no dateLastEdited, which Firestore's own server-side
    comparison also excludes.
    """
    url = f"{FIRESTORE_BASE}/apps/pro/users/{uid}/{collection}"
    headers = {"Authorization": f"Bearer {id_token}"}
    cutoff = float(last_edited_millis)

    documents: list[dict] = []
    page_token: Optional[str] = None
    while True:
        params: dict[str, Any] = {"pageSize": 300}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        code, detail = _firestore_error(resp)
        if code is not None:
            raise HTTPException(code, detail)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, detail or "Failed to fetch the collection")

        data = resp.json()
        if not isinstance(data, dict):
            raise HTTPException(502, f"documents:list returned {type(data).__name__}, expected an object")
        for document in data.get("documents", []):
            # A document without a name has no id, which is the document id.
            if isinstance(document, dict) and isinstance(document.get("name"), str):
                documents.append(document)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    filtered: list[dict] = []
    for document in documents:
        millis = _timestamp_millis(
            _firestore_field(_firestore_fields(document), "dateLastEdited")
        )
        # A document with no readable dateLastEdited never satisfies Firestore's
        # own >= comparison, so it is excluded here too.
        if millis is not None and millis >= cutoff:
            filtered.append(document)
    return filtered


def _fetch_document(id_token: str, uid: str, collection: str, doc_id: str) -> Optional[dict]:
    """Reads a single document by id via `documents:get`.

    Used for a known roastID. It is one round trip that reads one document,
    where `_fetch_collection` pages the whole collection and filters
    client-side -- the same address shape, so both hit the same proven path.

    Returns None when the document does not exist: `documents:get` reports a
    missing id as HTTP 404, which is the single-document equivalent of the
    listing simply omitting it.
    """
    url = f"{FIRESTORE_BASE}/apps/pro/users/{uid}/{collection}/{doc_id}"
    headers = {"Authorization": f"Bearer {id_token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404:
        return None
    code, detail = _firestore_error(resp)
    if code is not None:
        raise HTTPException(code, detail)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail or "Failed to fetch the document")

    document = resp.json()
    # Same normalisation as the listing: without a name there is no id, and an
    # odd body must not be mistaken for a roast.
    if not isinstance(document, dict) or not isinstance(document.get("name"), str):
        return None
    return document


def fetch_roast_logs(
    id_token: str,
    last_edited_millis: int = 0,
    parse_roast_data: bool = False,
    only_roasts_with_roaster_id: bool = False,
    omit_raw_roast_data: bool = False,
    profile_uuid: Optional[str] = None,
) -> list[RoastLogResponse]:
    uid = get_uid(id_token)
    roasts = _fetch_collection(id_token, uid, ROAST_LOG_COLLECTION, last_edited_millis)

    wanted_profile = _normalize_profile_uuid(profile_uuid) if profile_uuid else ""
    if wanted_profile:
        # Both sides are canonicalized before comparing: writes go through
        # _normalize_profile_uuid, but a document written by the Android app is
        # whatever FirestoreRoast.profileUUID held, so a dashed or lowercase uuid
        # in the document still matches the uuid the caller passed.
        def matches_profile(document: dict) -> bool:
            raw = _firestore_field(_firestore_fields(document), "profileUUID")
            if raw is None:
                return False
            return _normalize_profile_uuid(str(raw)) == wanted_profile

        roasts = [doc for doc in roasts if matches_profile(doc)]

    if only_roasts_with_roaster_id:
        # Reproduces FirestoreRoastLogsImpl.getUserRoastLogs, which keeps only the
        # roasts whose roasterID parses as an int (getRoasterID() returns -1
        # otherwise, and the app drops those).
        #
        # Off by default: in a real account this discards every roast whose
        # roasterID is null -- i.e. the whole oldest generation of logs -- while
        # still keeping the later ones, whose roasterID is a machine identifier
        # such as "580152". So this flag really means "roasts that carry a
        # roaster/machine id", not "roasts from a known machine".
        def has_roaster_id(document: dict) -> bool:
            raw = _firestore_field(_firestore_fields(document), "roasterID")
            try:
                return int(str(raw).strip()) != -1
            except (TypeError, ValueError):
                return False

        roasts = [doc for doc in roasts if has_roaster_id(doc)]

    notes_by_id: dict[str, RoastNotesResponse] = {}
    if roasts:
        # The app applies the same lastEditedMillisFilter to both collections
        # (getUserRoastLogs / getUserRoastNotes), so notes follow the roasts.
        note_docs = _fetch_collection(id_token, uid, ROAST_NOTES_COLLECTION, last_edited_millis)
        for document in note_docs:
            notes = _roast_notes_from_fields(_firestore_fields(document))
            notes_by_id[document["name"].split("/")[-1]] = notes

    logs: list[RoastLogResponse] = []
    for document in roasts:
        log = _roast_log_from_document(document, parse_roast_data, omit_raw_roast_data)
        log.notes = notes_by_id.get(log.roastID)
        logs.append(log)
    return logs


def fetch_roast_log(
    id_token: str,
    roast_id: str,
    parse_roast_data: bool = False,
    omit_raw_roast_data: bool = False,
) -> Optional[RoastLogResponse]:
    """Reads one roast log by its document id, notes joined. None if absent.

    Deliberately not filtered: the caller named the roast, so `lastEditedMillis`
    and `onlyRoastsWithRoasterID` do not apply here. Reusing them would only add
    a way for an existing roast to turn into a 404.
    """
    _validate_document_id(roast_id)
    uid = get_uid(id_token)
    document = _fetch_document(id_token, uid, ROAST_LOG_COLLECTION, roast_id)
    if document is None:
        return None

    log = _roast_log_from_document(document, parse_roast_data, omit_raw_roast_data)
    note_doc = _fetch_document(id_token, uid, ROAST_NOTES_COLLECTION, log.roastID)
    if note_doc is not None:
        log.notes = _roast_notes_from_fields(_firestore_fields(note_doc))
    return log


app = FastAPI(title="IKAWA Profiles API")

# Declared as a security scheme so Swagger exposes an "Authorize" button and
# sends "Authorization: Bearer <idToken>" to every protected endpoint.
bearer_scheme = HTTPBearer()


def _id_token(credentials: HTTPAuthorizationCredentials) -> str:
    return credentials.credentials


@app.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    return firebase_login(req.email, req.password)


@app.get("/profiles", response_model=list[ProfileResponse])
def get_profiles(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    return fetch_profiles(_id_token(credentials))


@app.post("/profiles/import")
def import_profile_endpoint(
    req: ImportRequest, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
):
    return import_profile(_id_token(credentials), req.shareUrl)


@app.post("/profiles/import/bulk", response_model=BulkImportResponse)
def import_bulk_endpoint(
    req: BulkImportRequest, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
):
    return import_profiles_bulk(
        _id_token(credentials), req.shareUrls, req.maxConcurrency, req.skipDuplicates
    )


@app.get("/roastlog", response_model=list[RoastLogResponse])
def get_roast_logs(
    lastEditedMillis: int = 0,
    parseRoastData: bool = False,
    onlyRoastsWithRoasterID: bool = False,
    omitRawRoastData: bool = False,
    profileUUID: Optional[str] = None,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    logs = fetch_roast_logs(
        _id_token(credentials),
        lastEditedMillis,
        parseRoastData,
        onlyRoastsWithRoasterID,
        omitRawRoastData,
        profileUUID,
    )
    # Serialized here instead of letting FastAPI do it from response_model.
    # With parseRoastData=true a roast carries ~700 points x 16 keys, and
    # FastAPI's serializer walks every leaf of that through jsonable_encoder
    # (~1.15M calls for 50 roasts) even though the values are already plain
    # float/str/None. model_dump_json uses Pydantic's Rust serializer instead:
    # same JSON, ~5x faster, and fewer bytes on the wire. Returning a Response
    # skips response_model validation; the shape is unchanged and no field
    # declares an alias, so OpenAPI still documents it from response_model.
    body = "[" + ",".join(log.model_dump_json() for log in logs) + "]"
    return Response(content=body, media_type="application/json")


@app.post("/roastlog/import")
def import_roast_endpoint(
    req: RoastLogImportRequest, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
):
    return import_roast(_id_token(credentials), req)


@app.post("/roastlog/import/bulk", response_model=RoastLogBulkImportResponse)
def import_roast_bulk_endpoint(
    req: RoastLogBulkImportRequest, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
):
    return import_roasts_bulk(
        _id_token(credentials), req.roasts, req.maxConcurrency, req.skipDuplicates
    )


@app.get("/roastlog/diagnose")
def diagnose_roast_log(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """Reports what Firestore actually returns, stage by stage.

    Diagnostic only: it performs the same read as /roastlog but returns the
    intermediate counts and the raw shape of the first document, so a silent
    empty result can be traced to auth, an empty collection, the date filter,
    or the roasterID filter.
    """
    id_token = _id_token(credentials)
    out: dict[str, Any] = {}

    try:
        uid = get_uid(id_token)
    except HTTPException as e:
        return {"ok": False, "stage": "token", "error": str(e.detail)}
    out["uid"] = uid

    for label, collection in (
        ("roasts", ROAST_LOG_COLLECTION),
        ("notes", ROAST_NOTES_COLLECTION),
    ):
        url = f"{FIRESTORE_BASE}/apps/pro/users/{uid}/{collection}"
        headers = {"Authorization": f"Bearer {id_token}"}
        resp = requests.get(url, headers=headers, params={"pageSize": 5}, timeout=60)
        entry: dict[str, Any] = {
            "url": url,
            "httpStatus": resp.status_code,
        }
        try:
            payload = resp.json()
        except ValueError:
            entry["body"] = resp.text[:800]
            out[label] = entry
            continue

        if not isinstance(payload, dict):
            entry["bodyType"] = type(payload).__name__
            entry["body"] = str(payload)[:800]
            out[label] = entry
            continue

        if "error" in payload:
            entry["error"] = payload["error"]
            out[label] = entry
            continue

        docs = payload.get("documents", [])
        entry["documentsReturned"] = len(docs)
        entry["hasNextPageToken"] = bool(payload.get("nextPageToken"))
        dated = 0
        roaster_ok = 0
        sample_roaster_ids: list[Any] = []
        if docs:
            entry["firstDocumentName"] = docs[0].get("name")
            entry["firstFieldKeys"] = sorted(_firestore_fields(docs[0]).keys())
            entry["firstFieldTypes"] = {
                key: next(iter(value), None)
                for key, value in sorted(_firestore_fields(docs[0]).items())
                if isinstance(value, dict)
            }
            entry["rawDateLastEdited"] = (
                _firestore_field(_firestore_fields(docs[0]), "dateLastEdited")
            )
        for document in docs:
            fields = _firestore_fields(document)
            if _timestamp_millis(_firestore_field(fields, "dateLastEdited")) is not None:
                dated += 1
            raw = _firestore_field(fields, "roasterID")
            sample_roaster_ids.append(raw)
            try:
                if int(str(raw).strip()) != -1:
                    roaster_ok += 1
            except (TypeError, ValueError):
                pass
        entry["withReadableDateLastEdited"] = dated
        entry["withUsableRoasterID"] = roaster_ok
        entry["sampleRoasterIDs"] = sample_roaster_ids
        # profileUUID drives GET /roastlog?profileUUID=. Reporting the raw values
        # shows the shape they really have in this account (dashed, lowercase,
        # absent), which is what decides whether that filter matches.
        raw_profiles = [_firestore_field(_firestore_fields(d), "profileUUID") for d in docs]
        entry["sampleProfileUUIDs"] = raw_profiles
        entry["distinctProfileUUIDs"] = sorted({str(p) for p in raw_profiles if p is not None})
        entry["roastsWithoutProfileUUID"] = sum(1 for p in raw_profiles if p is None)
        # Empty and absent values are excluded: they are not a canonical uuid,
        # they are a missing one, and counting them would make this look healthy.
        entry["profileUUIDMatchesCanonicalForm"] = sum(
            1
            for p in raw_profiles
            if p is not None and str(p) != "" and _normalize_profile_uuid(str(p)) == str(p)
        )
        out[label] = entry

    out["ok"] = True
    return out


# Registered after /roastlog/diagnose on purpose: FastAPI matches routes in
# declaration order, so a literal /roastlog/diagnose must keep winning over the
# {roastID} pattern.
@app.get("/roastlog/{roastID}", response_model=RoastLogResponse)
def get_roast_log_by_id(
    roastID: str,
    parseRoastData: bool = False,
    omitRawRoastData: bool = False,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """Reads a single roast log by its document id, notes joined.

    Reads the document directly instead of listing the collection, so it costs
    one round trip whatever the account size. 404 when the id does not exist in
    the authenticated user's collection -- documents are namespaced per uid, so
    another account's roast id is simply not found.
    """
    log = fetch_roast_log(
        _id_token(credentials), roastID, parseRoastData, omitRawRoastData
    )
    if log is None:
        raise HTTPException(404, f"roastID not found: {roastID!r}")
    body = log.model_dump_json()
    return Response(content=body, media_type="application/json")


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {
        "name": "IKAWA Profiles API",
        "version": "1.0.0",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)