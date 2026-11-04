import json
import os
import sqlite3
import uuid
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "recipes.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
CATEGORIES = ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert"]

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-in-production"
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload limit


# ---------- Database helpers ----------

def get_db():
    """Open one database connection per request and reuse it."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row  # lets us use row["title"]
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the recipes table if it doesn't exist yet."""
    with sqlite3.connect(DATABASE) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                ingredients TEXT NOT NULL,
                instructions TEXT NOT NULL,
                image TEXT,
                category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Databases created before categories existed don't have the column yet, so add it
        columns = [row[1] for row in db.execute("PRAGMA table_info(recipes)")]
        if "category" not in columns:
            db.execute("ALTER TABLE recipes ADD COLUMN category TEXT")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def read_recipe_form():
    """Read and check the add/edit form. Returns (data, error message or None)."""
    data = {
        "title": request.form.get("title", "").strip(),
        "ingredients": request.form.get("ingredients", "").strip(),
        "instructions": request.form.get("instructions", "").strip(),
        "category": request.form.get("category", ""),
    }
    if data["category"] not in CATEGORIES:
        data["category"] = None  # "No category" or anything unexpected
    if not data["title"] or not data["ingredients"] or not data["instructions"]:
        return data, "Please fill in the title, ingredients and instructions."
    return data, None


def save_uploaded_image(image_file):
    """Save an uploaded photo. Returns (filename, error message or None)."""
    if not allowed_file(image_file.filename):
        return None, "Image must be a PNG, JPG, GIF or WEBP file."
    # A random prefix stops two uploads called "photo.jpg" overwriting each other
    filename = f"{uuid.uuid4().hex}_{secure_filename(image_file.filename)}"
    image_file.save(os.path.join(UPLOAD_FOLDER, filename))
    return filename, None


def delete_image_file(filename):
    if filename:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            os.remove(path)


# ---------- TheMealDB helpers ----------

MEALDB_URL = "https://www.themealdb.com/api/json/v1/1/"  # "1" is the free test key


def mealdb_get(endpoint, **params):
    """Call TheMealDB and return its list of meals (empty list if nothing found)."""
    url = MEALDB_URL + endpoint
    if params:
        url += "?" + urlencode(params)
    request_ = Request(url, headers={"User-Agent": "my-recipe-app"})
    with urlopen(request_, timeout=10) as response:
        data = json.load(response)
    return data.get("meals") or []


def mealdb_to_recipe(meal):
    """Turn one TheMealDB meal into the fields our recipes table uses."""
    # TheMealDB stores ingredients as strIngredient1..20 with matching strMeasure1..20
    ingredients = []
    for i in range(1, 21):
        name = (meal.get(f"strIngredient{i}") or "").strip()
        measure = (meal.get(f"strMeasure{i}") or "").strip()
        if name:
            ingredients.append(f"{measure} {name}".strip())

    instructions = (meal.get("strInstructions") or "").replace("\r\n", "\n").strip()
    tags = " · ".join(t for t in (meal.get("strArea"), meal.get("strCategory")) if t)
    if tags:
        instructions = f"{tags}\n\n{instructions}"

    # TheMealDB categories are mostly main ingredients (Beef, Chicken, Pasta...), so map them
    mealdb_category = meal.get("strCategory") or ""
    if mealdb_category in ("Breakfast", "Dessert"):
        category = mealdb_category
    elif mealdb_category in ("Side", "Starter", "Miscellaneous", ""):
        category = None
    else:
        category = "Dinner"

    return {
        "title": meal["strMeal"].strip(),
        "ingredients": "\n".join(ingredients),
        "instructions": instructions,
        "category": category,
        "image_url": meal.get("strMealThumb"),
    }


def download_image(url):
    """Save a remote image into the uploads folder and return its filename (or None)."""
    if not url:
        return None
    try:
        with urlopen(Request(url, headers={"User-Agent": "my-recipe-app"}), timeout=10) as response:
            if not response.headers.get("Content-Type", "").startswith("image/"):
                return None
            data = response.read(5 * 1024 * 1024)
    except (URLError, OSError):
        return None
    extension = url.rsplit(".", 1)[-1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        extension = "jpg"
    filename = f"{uuid.uuid4().hex}_mealdb.{extension}"
    with open(os.path.join(UPLOAD_FOLDER, filename), "wb") as f:
        f.write(data)
    return filename


# ---------- Routes ----------

@app.route("/")
def index():
    """Show all recipes, filtered by the search box and/or a category."""
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "")
    if category not in CATEGORIES:
        category = ""

    # Build the WHERE part step by step so search and category can be combined
    conditions, params = [], []
    if query:
        conditions.append("(title LIKE ? OR ingredients LIKE ?)")
        params += [f"%{query}%", f"%{query}%"]
    if category:
        conditions.append("category = ?")
        params.append(category)

    sql = "SELECT * FROM recipes"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC"

    recipes = get_db().execute(sql, params).fetchall()
    return render_template("index.html", recipes=recipes, query=query,
                           category=category, categories=CATEGORIES)


@app.route("/add", methods=["GET", "POST"])
def add_recipe():
    """Show the form (GET) or save a new recipe (POST)."""
    if request.method == "POST":
        data, error = read_recipe_form()

        image_name = None
        image_file = request.files.get("image")
        if not error and image_file and image_file.filename:
            image_name, error = save_uploaded_image(image_file)

        if error:
            flash(error)
            return render_template("add.html", form=request.form, recipe=None, categories=CATEGORIES)

        db = get_db()
        cursor = db.execute(
            "INSERT INTO recipes (title, ingredients, instructions, category, image) "
            "VALUES (?, ?, ?, ?, ?)",
            (data["title"], data["ingredients"], data["instructions"], data["category"], image_name),
        )
        db.commit()
        flash("Recipe added!")
        return redirect(url_for("view_recipe", recipe_id=cursor.lastrowid))

    return render_template("add.html", form={}, recipe=None, categories=CATEGORIES)


@app.route("/recipe/<int:recipe_id>/edit", methods=["GET", "POST"])
def edit_recipe(recipe_id):
    """Show the form filled in with the recipe (GET) or save the changes (POST)."""
    db = get_db()
    recipe = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if recipe is None:
        abort(404)

    if request.method == "POST":
        data, error = read_recipe_form()

        image_name = recipe["image"]
        image_file = request.files.get("image")
        if not error and image_file and image_file.filename:
            # A new photo replaces the old one
            new_image, error = save_uploaded_image(image_file)
            if not error:
                delete_image_file(image_name)
                image_name = new_image
        elif not error and request.form.get("remove_image"):
            delete_image_file(image_name)
            image_name = None

        if error:
            flash(error)
            return render_template("add.html", form=request.form, recipe=recipe, categories=CATEGORIES)

        db.execute(
            "UPDATE recipes SET title = ?, ingredients = ?, instructions = ?, category = ?, image = ? "
            "WHERE id = ?",
            (data["title"], data["ingredients"], data["instructions"], data["category"],
             image_name, recipe_id),
        )
        db.commit()
        flash("Recipe updated!")
        return redirect(url_for("view_recipe", recipe_id=recipe_id))

    # dict() turns the database row into something the template can call .get() on
    return render_template("add.html", form=dict(recipe), recipe=recipe, categories=CATEGORIES)


@app.route("/spin")
def spin():
    """A spinning wheel that picks a random recipe, optionally from one category."""
    category = request.args.get("category", "")
    if category not in CATEGORIES:
        category = ""

    if category:
        rows = get_db().execute(
            "SELECT id, title, image FROM recipes WHERE category = ? ORDER BY title", (category,)
        ).fetchall()
    else:
        rows = get_db().execute("SELECT id, title, image FROM recipes ORDER BY title").fetchall()
    recipes = [
        {
            "title": r["title"],
            "url": url_for("view_recipe", recipe_id=r["id"]),
            "image": url_for("static", filename="uploads/" + r["image"]) if r["image"] else None,
        }
        for r in rows
    ]
    return render_template("spin.html", recipes=recipes, category=category, categories=CATEGORIES)


@app.route("/discover")
def discover():
    """Search TheMealDB (or get a random meal) and show the results."""
    query = request.args.get("q", "").strip()
    surprise = "random" in request.args
    meals = []
    error = None

    if query or surprise:
        try:
            meals = mealdb_get("random.php") if surprise else mealdb_get("search.php", s=query)
        except (URLError, OSError, ValueError):
            error = "Couldn't reach TheMealDB. Check your internet connection and try again."

    # Remember which ones are already saved so we can show "Saved" instead of the button
    saved = {row["title"].lower() for row in get_db().execute("SELECT title FROM recipes")}
    results = [
        {
            "id": m["idMeal"],
            "title": m["strMeal"],
            "image": m.get("strMealThumb"),
            "info": " · ".join(t for t in (m.get("strArea"), m.get("strCategory")) if t),
            "saved": m["strMeal"].strip().lower() in saved,
        }
        for m in meals
    ]
    return render_template("discover.html", results=results, query=query,
                           searched=bool(query or surprise), error=error)


@app.route("/discover/save/<meal_id>", methods=["POST"])
def save_from_mealdb(meal_id):
    """Fetch the full recipe from TheMealDB and store it in our own database."""
    try:
        meals = mealdb_get("lookup.php", i=meal_id)
    except (URLError, OSError, ValueError):
        flash("Couldn't reach TheMealDB. Please try again.")
        return redirect(request.referrer or url_for("discover"))
    if not meals:
        abort(404)

    recipe = mealdb_to_recipe(meals[0])
    db = get_db()

    existing = db.execute("SELECT id FROM recipes WHERE lower(title) = lower(?)",
                          (recipe["title"],)).fetchone()
    if existing:
        flash("You've already saved this recipe.")
        return redirect(url_for("view_recipe", recipe_id=existing["id"]))

    image_name = download_image(recipe["image_url"])
    cursor = db.execute(
        "INSERT INTO recipes (title, ingredients, instructions, category, image) "
        "VALUES (?, ?, ?, ?, ?)",
        (recipe["title"], recipe["ingredients"], recipe["instructions"], recipe["category"],
         image_name),
    )
    db.commit()
    flash(f"Saved “{recipe['title']}” to your recipes!")
    return redirect(url_for("view_recipe", recipe_id=cursor.lastrowid))


@app.route("/recipe/<int:recipe_id>")
def view_recipe(recipe_id):
    recipe = get_db().execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if recipe is None:
        abort(404)
    return render_template("recipe.html", recipe=recipe)


@app.route("/recipe/<int:recipe_id>/delete", methods=["POST"])
def delete_recipe(recipe_id):
    db = get_db()
    recipe = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if recipe is None:
        abort(404)
    delete_image_file(recipe["image"])
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    db.commit()
    flash("Recipe deleted.")
    return redirect(url_for("index"))


@app.errorhandler(413)
def too_large(e):
    flash("That image is too large (max 5 MB).")
    return redirect(url_for("add_recipe"))


os.makedirs(UPLOAD_FOLDER, exist_ok=True)
init_db()

if __name__ == "__main__":
    app.run(debug=True)