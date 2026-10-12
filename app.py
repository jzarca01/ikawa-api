import base64
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

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
        data = resp.json()
        if resp.status_code != 200:
            raise HTTPException(403, data.get("error", {}).get("message", "Failed to fetch profiles"))
        documents.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    profiles = []
    for doc in documents:
        fields = doc.get("fields", {})
        profile = ProfileResponse(
            uuid=doc["name"].split("/")[-1],
            userID=fields.get("userID", {}).get("stringValue", ""),
            name=fields.get("name", {}).get("stringValue", ""),
            coffeeName=fields.get("coffeeName", {}).get("stringValue"),
            coffeeID=fields.get("coffeeID", {}).get("stringValue"),
            roastTempPoints=fields.get("roastTempPoints", {}).get("stringValue"),
            roastFanPoints=fields.get("roastFanPoints", {}).get("stringValue"),
            isArchived=fields.get("isArchived", {}).get("booleanValue", False),
            isFavourite=fields.get("isFavourite", {}).get("booleanValue", False),
            showInLibrary=fields.get("showInLibrary", {}).get("booleanValue", True),
            schemeVersion=int(fields.get("schemeVersion", {}).get("integerValue", "1")),
            revision=int(fields.get("revision", {}).get("integerValue", "0")),
            tempSensor=int(fields.get("tempSensor", {}).get("integerValue", "0")),
            cooldownPointTime=float(fields.get("cooldownPointTime", {}).get("doubleValue", 0.0)),
            cooldownPointPower=float(fields.get("cooldownPointPower", {}).get("doubleValue", 0.0)),
            dateCreated=fields.get("dateCreated", {}).get("timestampValue"),
            dateLastEdited=fields.get("dateLastEdited", {}).get("timestampValue"),
            dateLastRoasted=fields.get("dateLastRoasted", {}).get("timestampValue"),
            syncedTimestamp=fields.get("syncedTimestamp", {}).get("timestampValue"),
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