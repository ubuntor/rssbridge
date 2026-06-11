# Copyright 2024: A. Fontenot (https://github.com/afontenot)
# SPDX-License-Identifier: MPL-2.0
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import (
    Flask,
    Response,
    abort,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from jinja2 import pass_eval_context
from markupsafe import Markup, escape
from werkzeug.exceptions import NotFound

REFETCH_HANDLES_SECS = 86400 * 7
REFETCH_PROFILES_SECS = 86400 * 7
CACHE_NONEXISTENT_HANDLES_SECS = 86400
CACHE_POSTS_SECS = 3600
CACHE_DIR = "cache"
DATABASE = "bsky.db"
MAX_POSTS_IN_FEED = 100
MIN_POSTS_IN_FEED = 30
FEED_FETCH_SIZE = 30  # >=1, <=100

# anti-feature?
SKIP_AUTH_REQ_POSTS = False

# constants
PROFILE_URL = "https://bsky.app/profile"
IMAGE_URL = "https://cdn.bsky.app/img/feed_fullsize/plain"
VIDEO_URL = "https://video.bsky.app/watch"
BSKY_PUBLIC_API = "https://public.api.bsky.app/xrpc"
VALID_HANDLE_REGEX = re.compile(
    r"^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)
VALID_FILTERS = [
    "posts_and_author_threads",
    "posts_with_replies",
    "posts_no_replies",
    "posts_with_media",
]
DEFAULT_FILTER = "posts_and_author_threads"

app = Flask(__name__)
iso = datetime.fromisoformat
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)


@app.template_filter()
@pass_eval_context
def nl2br(eval_ctx, value):
    assert eval_ctx.autoescape
    br = Markup("<br>")
    result = br.join(value.split("\n"))
    return Markup(result)


class BskyXrpcClient:
    def __init__(self):
        self.s = requests.Session()

    def get_posts(self, actor, post_filter, server_url=BSKY_PUBLIC_API, last=None):
        url = f"{server_url}/app.bsky.feed.getAuthorFeed"
        params = {
            "actor": actor,
            "filter": post_filter,
            "limit": FEED_FETCH_SIZE,
        }

        now = datetime.now(timezone.utc)
        earliest_post_date = now
        posts = []
        while True:
            r = self.s.get(url, params=params)
            r.raise_for_status()
            feed = r.json()["feed"]

            for item in feed:
                post = item["post"]
                # reposts make the post date not monotonic...
                post_date = get_post_date(post)
                if post_date < now:
                    earliest_post_date = post_date
                if "reply" in item:
                    post["reply"] = item["reply"]
                if "reason" in item:
                    post["reason"] = item["reason"]
                posts.append(post)
                if len(posts) >= MIN_POSTS_IN_FEED and (
                    (last and earliest_post_date <= last)
                    or len(posts) >= MAX_POSTS_IN_FEED
                ):
                    return posts

            # on first fetch, only return up to 1 batch of posts
            if not last:
                return posts

            # end of posts
            if len(feed) < FEED_FETCH_SIZE:
                return posts

            params.update(
                {"cursor": earliest_post_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
            )

    def get_actor(self, handle, server_url=BSKY_PUBLIC_API):
        url = f"{server_url}/com.atproto.identity.resolveHandle"
        params = {"handle": handle}
        r = self.s.get(url, params=params)
        r.raise_for_status()
        return r.json()["did"]

    def get_profile(self, actor, server_url=BSKY_PUBLIC_API):
        url = f"{server_url}/app.bsky.actor.getProfile"
        params = {
            "actor": actor,
        }
        r = self.s.get(url, params=params)
        r.raise_for_status()
        return r.json()


def at_uri_to_url(uri):
    author_did = uri[uri.index("did:") :].split("/")[0]
    post_stub = uri.split("/")[-1]
    # FIXME: improve this hardcoded link?
    return f"{PROFILE_URL}/{author_did}/post/{post_stub}"


def format_author(author):
    if "displayName" not in author or not author["displayName"]:
        return author["handle"]
    return f"{author['displayName']} ({author['handle']})"


def get_media_embeds(embed, author_did):
    embeds = []
    match embed["$type"]:
        case "app.bsky.embed.gallery#view" | "app.bsky.embed.gallery":
            return [j for i in embed["items"] for j in get_media_embeds(i, author_did)]
        case "app.bsky.embed.images#view":
            for image in embed["images"]:
                alt = image["alt"]
                src = image["fullsize"]
                embeds.append(
                    {
                        "type": "image",
                        "url": src,
                        "alt": alt,
                    }
                )
        case "app.bsky.embed.gallery#viewImage":
            alt = embed["alt"]
            src = embed["fullsize"]
            embeds.append(
                {
                    "type": "image",
                    "url": src,
                    "alt": alt,
                }
            )
        case "app.bsky.embed.images":
            for image in embed["images"]:
                alt = image["alt"]
                # FIXME: hardcoded...
                src = f"{IMAGE_URL}/{author_did}/{image['image']['ref']['$link']}@jpeg"
                embeds.append(
                    {
                        "type": "image",
                        "url": src,
                        "alt": alt,
                    }
                )
        case "app.bsky.embed.gallery#image":
            alt = embed["alt"]
            # FIXME: hardcoded...
            src = f"{IMAGE_URL}/{author_did}/{embed['image']['ref']['$link']}@jpeg"
            embeds.append(
                {
                    "type": "image",
                    "url": src,
                    "alt": alt,
                }
            )
        case "app.bsky.embed.video#view":
            embeds.append(
                {
                    "type": "video",
                    "thumbnail": embed["thumbnail"],
                    "playlist": embed["playlist"],
                }
            )
        case "app.bsky.embed.video":
            # FIXME: hardcoded...
            video_link = embed["video"]["ref"]["$link"]
            embeds.append(
                {
                    "type": "video",
                    "thumbnail": f"{VIDEO_URL}/{author_did}/{video_link}/thumbnail.jpg",
                    "playlist": f"{VIDEO_URL}/{author_did}/{video_link}/playlist.m3u8",
                }
            )
    return embeds


def get_post_date(post):
    if "reason" in post and "by" in post["reason"]:
        # FIXME: we don't have the repost date... https://github.com/bluesky-social/atproto/discussions/2702
        # using indexedAt as the next best thing
        return iso(post["reason"]["indexedAt"])
    else:
        return iso(post["record"]["createdAt"])


def get_post_metadata(post, actor):
    data = {
        "author": post["author"]["did"],
        "authorHandle": post["author"]["handle"],
        "authorName": post["author"].get("displayName", ""),
        "original_date": iso(post["record"]["createdAt"]),
        "date": get_post_date(post),
        "text": post["record"]["text"],
        "url": at_uri_to_url(post["uri"]),
        "categories": [],
    }
    if "reason" in post and "by" in post["reason"]:
        if post["author"]["did"] == actor:
            data["categories"].append("self-repost")
        else:
            data["categories"].append("repost")
    data["title"] = ""
    post_text = ""
    if "text" in post["record"] and post["record"]["text"] != "":
        post_text = post["record"]["text"]

    if "embed" in post:
        match post["embed"]["$type"]:
            case "app.bsky.embed.images#view":
                data["categories"].append("image")
            case "app.bsky.embed.video#view":
                data["categories"].append("video")
            case "app.bsky.embed.gallery#view":
                if any(
                    item["$type"] == "app.bsky.embed.gallery#viewImage"
                    for item in post["embed"]["items"]
                ):
                    data["categories"].append("image")
        if "record" in post["embed"]:
            if post["embed"]["$type"] == "app.bsky.embed.record#view":
                record = post["embed"]["record"]
            elif post["embed"]["$type"] == "app.bsky.embed.recordWithMedia#view":
                match post["embed"]["media"]["$type"]:
                    case "app.bsky.embed.images#view":
                        data["categories"].append("image")
                    case "app.bsky.embed.video#view":
                        data["categories"].append("video")
                record = post["embed"]["record"]["record"]
            match record["$type"]:
                case "app.bsky.embed.record#viewNotFound":
                    data["categories"].append("quote")
                    data["title"] = "Quoted deleted post: "
                case "app.bsky.embed.record#viewDetached":
                    data["categories"].append("quote")
                    data["title"] = "Quoted detached post: "
                case "app.bsky.embed.record#viewBlocked":
                    data["categories"].append("quote")
                    data["title"] = "Quoted blocked post: "
                case _:
                    if "author" in record:
                        if record["author"]["did"] == actor:
                            data["categories"].append("self-quote")
                            data["title"] = "Self-quoted: "
                        else:
                            data["categories"].append("quote")
                            author = format_author(record["author"])
                            data["title"] = f"Quoted {author}: "

    # reply takes precedence over quotes in the title
    if "reply" in post:
        if post["reply"]["parent"]["$type"] == "app.bsky.feed.defs#notFoundPost":
            data["title"] = "Replied to deleted post: "
            data["categories"].append("reply")
        elif post["reply"]["parent"]["$type"] == "app.bsky.feed.defs#blockedPost":
            data["title"] = "Replied to blocked post: "
            data["categories"].append("reply")
        elif post["reply"]["parent"]["author"]["did"] == actor:
            data["title"] = f"Self-replied: "
            data["categories"].append("self-reply")
        else:
            author = format_author(post["reply"]["parent"]["author"])
            data["title"] = f"Replied to {author}: "
            data["categories"].append("reply")
    data["categories"] = sorted(set(data["categories"]))

    if post_text == "":
        if "image" in data["categories"]:
            post_text = "(image)"
        if "video" in data["categories"]:
            post_text = "(video)"
    data["title"] += post_text
    return data


def post_to_html(post, author_did):
    segments = []
    embeds = []
    if "embed" in post:
        embeds = [post["embed"]]
    elif "embeds" in post:
        embeds = post["embeds"]
    elif "record" in post and "embed" in post["record"]:
        embeds = [post["record"]["embed"]]

    for embed in embeds:
        media_embeds = get_media_embeds(embed, author_did)
        if media_embeds:
            segments.extend(media_embeds)
        elif embed["$type"] == "app.bsky.embed.external#view":
            segments.append(
                {
                    "type": "extlink",
                    "thumbnail": embed["external"].get("thumb", ""),
                    "url": embed["external"]["uri"],
                    "text": embed["external"]["title"] or embed["external"]["uri"],
                    "description": embed["external"]["description"]
                    or embed["external"]["uri"],
                }
            )
        elif embed["$type"] in {
            "app.bsky.embed.record#view",
            "app.bsky.embed.record",
            "app.bsky.embed.recordWithMedia#view",
            "app.bsky.embed.recordWithMedia",
        }:
            # image or video quoted-posted
            if embed["$type"] in {
                "app.bsky.embed.recordWithMedia#view",
                "app.bsky.embed.recordWithMedia",
            }:
                segments.extend(get_media_embeds(embed["media"], author_did))
                if "$type" not in embed["record"]:
                    embed["record"] = embed["record"]["record"]
            if embed["$type"] == "app.bsky.embed.record":
                segments.insert(
                    0,
                    {
                        "type": "quotepost",
                        "url": at_uri_to_url(embed["record"]["uri"]),
                    },
                )
            elif embed["$type"] == "app.bsky.embed.recordWithMedia":
                segments.insert(
                    0,
                    {
                        "type": "quotepost",
                        "url": at_uri_to_url(embed["record"]["record"]["uri"]),
                    },
                )
            elif embed["record"]["$type"] == "app.bsky.embed.record#viewNotFound":
                segments.insert(
                    0,
                    {
                        "type": "placeholder",
                        "text": "(quote of deleted post)",
                    },
                )
            elif embed["record"]["$type"] == "app.bsky.embed.record#viewDetached":
                segments.insert(
                    0,
                    {
                        "type": "placeholder",
                        "text": "(quote of detached post)",
                    },
                )
            elif embed["record"]["$type"] == "app.bsky.embed.record#viewBlocked":
                segments.insert(
                    0,
                    {
                        "type": "placeholder",
                        "text": "(quote of blocked post)",
                    },
                )
            elif "author" in embed["record"]:
                # some unhandled embeds, like starter packs, don't have authors
                author = embed["record"]["author"]
                embed["record"]["record"] = embed["record"]["value"]
                del embed["record"]["value"]
                segments.insert(
                    0,
                    {
                        "type": "quotepost",
                        "name": format_author(author),
                        "date": embed["record"]["record"]["createdAt"],
                        "url": at_uri_to_url(embed["record"]["uri"]),
                        "html": post_to_html(embed["record"], author["did"]),
                    },
                )
    if "reply" in post:
        reply_segment = {"type": "reply", "subsegs": []}
        for position in ["root", "parent"]:
            if position == "root":
                if post["reply"]["root"]["uri"] == post["reply"]["parent"]["uri"]:
                    continue
            match post["reply"][position]["$type"]:
                case "app.bsky.feed.defs#notFoundPost":
                    reply_segment["subsegs"].append(
                        {"type": "placeholder", "text": "(deleted post)"}
                    )
                case "app.bsky.feed.defs#blockedPost":
                    reply_segment["subsegs"].append(
                        {
                            "type": "placeholder",
                            "text": "(post by blocked account)",
                        }
                    )
                case "app.bsky.feed.defs#postView":
                    if "record" in post["reply"][position]:
                        author = post["reply"][position]["author"]
                        reply_segment["subsegs"].append(
                            {
                                "type": "post",
                                "name": format_author(author),
                                "date": post["reply"][position]["record"]["createdAt"],
                                "url": at_uri_to_url(post["reply"][position]["uri"]),
                                "html": post_to_html(
                                    post["reply"][position], author["did"]
                                ),
                            }
                        )
            if position == "root":
                if (
                    "record" in post["reply"]["parent"]
                    and "reply" in post["reply"]["parent"]["record"]
                    and post["reply"]["parent"]["record"]["reply"]["root"]["uri"]
                    == post["reply"]["parent"]["record"]["reply"]["parent"]["uri"]
                ):
                    # grandparent and root are the same
                    reply_segment["subsegs"].append({"type": "reply_gap", "html": ""})
                else:
                    reply_segment["subsegs"].append(
                        {"type": "reply_gap", "html": "&vellip;"}
                    )

        segments.insert(0, reply_segment)

    if "record" in post and "text" in post["record"]:
        text = post["record"]["text"]
        cursor = 0
        text_seg = {"type": "text", "subsegs": []}
        if "facets" in post["record"]:
            # FIXME: round-trip encoding sucks a lot, but is hard to avoid...
            btext = text.encode("utf-8")
            for facet in sorted(
                post["record"]["facets"], key=lambda x: x["index"]["byteStart"]
            ):
                if facet["features"][0]["$type"] == "app.bsky.richtext.facet#link":
                    text_seg["subsegs"].append(
                        {
                            "type": "text",
                            "value": btext[cursor : facet["index"]["byteStart"]].decode(
                                "utf-8", "surrogateescape"
                            ),
                        }
                    )
                    url = facet["features"][0]["uri"]
                    link_text = btext[
                        facet["index"]["byteStart"] : facet["index"]["byteEnd"]
                    ].decode("utf-8", "surrogateescape")
                    text_seg["subsegs"].append(
                        {
                            "type": "link",
                            "text": link_text,
                            "url": url,
                        }
                    )
                    cursor = facet["index"]["byteEnd"]
                elif facet["features"][0]["$type"] == "app.bsky.richtext.facet#mention":
                    text_seg["subsegs"].append(
                        {
                            "type": "text",
                            "value": btext[cursor : facet["index"]["byteStart"]].decode(
                                "utf-8", "surrogateescape"
                            ),
                        }
                    )
                    did = facet["features"][0]["did"]
                    url = f"{PROFILE_URL}/{did}"
                    link_text = btext[
                        facet["index"]["byteStart"] : facet["index"]["byteEnd"]
                    ].decode("utf-8", "surrogateescape")
                    text_seg["subsegs"].append(
                        {
                            "type": "link",
                            "text": link_text,
                            "url": url,
                        }
                    )
                    cursor = facet["index"]["byteEnd"]
            text_seg["subsegs"].append(
                {
                    "type": "text",
                    "value": btext[cursor:].decode("utf-8", "surrogateescape"),
                }
            )
        else:
            text_seg["subsegs"].append({"type": "text", "value": text})
        segments.append(text_seg)

    return render_template("post.html", segments=segments)


def actorfeed(actor: str) -> Response:
    client = get_client()

    post_filter = request.args.get("filter", DEFAULT_FILTER)
    if post_filter not in VALID_FILTERS:
        abort(400)

    conn = get_db()
    curs = conn.cursor()
    now = datetime.now(timezone.utc)
    latest_date = None

    res = curs.execute(
        "SELECT fetched, latest_date FROM fetches WHERE did = ? AND filter = ?",
        (actor, post_filter),
    ).fetchone()
    if res:
        fetched = iso(res[0])
        if res[1]:
            latest_date = iso(res[1])
        # if fetched less than an hour ago, return cached file
        post_age = (now - fetched).total_seconds()
        if post_age < CACHE_POSTS_SECS:
            try:
                return send_from_directory(
                    CACHE_DIR,
                    f"{actor}.{post_filter}.atom.xml",
                    max_age=CACHE_POSTS_SECS - post_age + 1,
                    mimetype="application/atom+xml",
                )
            except NotFound:
                pass

    # check last time actor feed was updated
    res = curs.execute("SELECT * FROM profiles WHERE did = ?", (actor,))
    res = res.fetchone()

    # we know about this actor already
    if res:
        profile = {
            "did": res[0],
            "handle": res[1],
            "name": res[2],
            "avatar": res[3],
            "description": res[4],
            "updated": res[5],
        }

    # never fetched before, verify actor and fetch posts
    if (
        not res
        or (now - iso(profile["updated"])).total_seconds() > REFETCH_PROFILES_SECS
    ):
        try:
            print("fetching profile for", actor)
            profile = client.get_profile(actor)
        except requests.HTTPError:
            abort(404)
        # option: don't allow fetching "login-required" profiles
        if SKIP_AUTH_REQ_POSTS and "labels" in profile:
            for label in profile["labels"]:
                if (
                    label.get("src") == profile["did"]
                    and label.get("val") == "!no-unauthenticated"
                ):
                    abort(404)

        # avatars can be missing...
        avatar = profile.get("avatar", "")
        # descriptions can be missing...
        description = profile.get("description", "")
        profile = {
            "did": actor,
            "handle": profile["handle"],
            "name": format_author(profile),
            "avatar": avatar,
            "description": description,
            "updated": now.isoformat(),
        }
        curs.execute(
            "INSERT OR REPLACE INTO profiles VALUES(:did, :handle, :name, :avatar,"
            " :description, :updated)",
            profile,
        )
        conn.commit()

    # add additional metadata not saved in database
    profile["url"] = f"{PROFILE_URL}/{profile['did']}"

    try:
        posts = client.get_posts(actor, post_filter, last=latest_date)
    except requests.HTTPError:
        abort(404)

    if not posts:
        abort(404)

    for post in posts:
        post_metadata = get_post_metadata(post, actor)
        # FIXME: look into updating edited posts
        postdata = curs.execute(
            "SELECT EXISTS(SELECT 1 FROM posts WHERE cid = ?)", (post["cid"],)
        )
        postdata = postdata.fetchone()
        if not postdata[0]:
            html = post_to_html(post, post_metadata["author"])
            data = {
                "cid": post["cid"],
                "did": post_metadata["author"],
                "url": post_metadata["url"],
                "html": html,
                "date": post_metadata["original_date"].isoformat(),
                "handle": post_metadata["authorHandle"],
                "name": post_metadata["authorName"],
                "title": post_metadata["title"],
            }
            # posts can contain surrogates??? what on earth
            data = {k: re.sub("[\ud800-\udfff]", "\ufffd", v) for k, v in data.items()}
            print(data)
            curs.execute(
                "INSERT INTO posts VALUES(:cid, :did, :url, :html, :date, :handle,"
                " :name, :title)",
                data,
            )
        data = {
            "did": actor,
            "cid": post["cid"],
            "updated": post_metadata["date"].isoformat(),
            "categories": ",".join(post_metadata["categories"]),
        }
        for f in VALID_FILTERS:
            data["filter_" + f] = f == post_filter
        # dedup self-reposts in favor of the repost
        curs.execute(
            "INSERT into feed_items VALUES(:did, :cid, :updated, :categories,"
            " :filter_posts_and_author_threads, :filter_posts_with_replies,"
            " :filter_posts_no_replies, :filter_posts_with_media) ON CONFLICT DO"
            f" UPDATE SET filter_{post_filter} = 1, updated = max(updated, :updated)",
            data,
        )
    conn.commit()

    data = {"did": actor, "filter": post_filter, "fetched": now.isoformat()}
    if posts != []:
        # just in case that the latest post has a spoofed post date in the far future, clamp to right now
        data["latest_date"] = min(
            max(map(get_post_date, posts)), datetime.now(timezone.utc)
        ).isoformat()
    curs.execute(
        "INSERT OR REPLACE INTO fetches VALUES(:did, :filter, :fetched, :latest_date)",
        data,
    )
    conn.commit()

    posts = curs.execute(
        "SELECT posts.cid, posts.did, posts.url, posts.html, posts.date, posts.handle,"
        " posts.name, posts.title, feed_items.updated, feed_items.categories FROM"
        " feed_items INNER JOIN posts USING (cid) WHERE feed_items.did = ? AND"
        f" filter_{post_filter} = 1 ORDER BY updated DESC LIMIT {MAX_POSTS_IN_FEED}",
        (actor,),
    )
    posts = posts.fetchall()

    posts_data = []

    for post in posts:
        author = format_author({"displayName": post[6], "handle": post[5]})
        title = post[7]
        categories = [] if post[9] == "" else post[9].split(",")
        if "self-repost" in categories:
            title = f"Self-reposted: {title}"
        elif "repost" in categories:
            title = f"Reposted {author}: {title}"
        posts_data.append(
            {
                "cid": post[0],
                "url": post[2],
                "html": post[3],
                "date": post[4],
                "updated": post[8],
                "author": author,
                "title": title,
                "categories": categories,
            }
        )

    feed_data = {
        "post_filter": post_filter,
        "url": request.url_root + f"feed/{actor}?filter={post_filter}",
    }

    ofs = render_template(
        "atom.xml", profile=profile, posts=posts_data, feed=feed_data
    ).encode("utf-8")

    with open(f"{CACHE_DIR}/{actor}.{post_filter}.atom.xml", "wb") as f:
        f.write(ofs)

    return send_from_directory(
        CACHE_DIR,
        f"{actor}.{post_filter}.atom.xml",
        max_age=CACHE_POSTS_SECS + 1,
        mimetype="application/atom+xml",
    )


def handlefeed(handle) -> Response:
    conn = get_db()
    curs = conn.cursor()
    res = curs.execute("SELECT did,updated FROM handles WHERE handle = ?", (handle,))
    res = res.fetchone()
    now = datetime.now(timezone.utc)

    if res:
        actor, updated = res
        if not actor:
            if (now - iso(updated)).total_seconds() < CACHE_NONEXISTENT_HANDLES_SECS:
                abort(404)
                # raise ValueError("requested cached non-existent handle too soon")

    if not res or (now - iso(updated)).total_seconds() > REFETCH_HANDLES_SECS:
        try:
            actor = get_client().get_actor(handle)
            updated = now
        except requests.HTTPError:
            abort(404)

        if actor:
            data = {"actor": actor, "handle": handle, "now": now.isoformat()}
            curs.execute(
                "INSERT OR REPLACE INTO handles VALUES(:handle, :actor, :now)", data
            )
            conn.commit()
        else:
            data = {"handle": handle, "now": now.isoformat()}
            curs.execute("INSERT OR REPLACE INTO handles VALUES(:handle, :now)", data)
            conn.commit()

    if actor:
        return redirect(url_for("feed", user=actor, filter=request.args.get("filter")))
    abort(404)


def get_client():
    client = getattr(g, "_client", None)
    if client is None:
        client = g._client = BskyXrpcClient()
    return client


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        with open("bsky.schema") as f:
            schema = f.read()
        db.executescript(schema)
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def is_valid_handle(handle):
    return len(handle) <= 253 and VALID_HANDLE_REGEX.match(handle)


def is_valid_did(actor):
    return len(actor) == 32 and actor.startswith("did:plc:") and actor[8:].isalnum()


@app.route("/feed/<user>")
def feed(user):
    if is_valid_handle(user):
        return handlefeed(user)
    if is_valid_did(user):
        return actorfeed(user)
    abort(404)


@app.route("/feed")
def bare_feed():
    user = request.args.get("user")
    return redirect(url_for("feed", user=user, filter=request.args.get("filter")))


@app.route("/")
def root():
    return render_template("root.html")
