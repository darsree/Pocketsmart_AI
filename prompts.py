"""Prompt templates for the three planners (Activity 1.3: budget interpretation + recommendations).

The JSON schemas match the structures used in the project document so the planner
modules (Home / Party / Jewelry) can plug these straight in.
"""

_HOME_SCHEMA = """{
  "total_budget": 0.0,
  "budget_breakdown": [
    {"category": "lighting", "allocation": 0.0,
     "items": [{"name": "", "description": "", "estimated_price": 0.0, "quantity": 0, "search_terms": ""}]}
  ],
  "calculation_table": [{"category": "", "items_count": 0, "total_cost": 0.0, "percentage_of_budget": 0.0}],
  "remaining_budget": 0.0,
  "additional_suggestions": []
}"""

_PARTY_SCHEMA = """{
  "total_budget": 0.0,
  "budget_breakdown": [
    {"category": "venue", "allocation": 0.0,
     "items": [{"name": "", "description": "", "estimated_price": 0.0, "quantity": 0, "search_terms": ""}]}
  ],
  "venue_suggestions": [{"name": "", "type": "", "capacity": 0, "estimated_cost": 0.0, "search_terms": ""}],
  "remaining_budget": 0.0,
  "additional_suggestions": []
}"""

_JEWELRY_SCHEMA = """{
  "outfit_analysis": {"colors": [], "style": "", "formality": ""},
  "total_budget": 0.0,
  "jewelry_recommendations": [
    {"item_type": "", "description": "", "style": "", "estimated_price": 0.0, "search_terms": ""}
  ],
  "remaining_budget": 0.0,
  "styling_tips": []
}"""

_RULES = (
    "Rules: use INR prices only, keep the sum of all estimated_price x quantity within the budget, "
    "recommend items available in India, give short search_terms usable on Indian shopping sites, "
    "and reply with ONLY valid JSON in exactly this structure:\n"
)


def home_prompt(total_budget: float, num_lights: int = 0, num_fans: int = 0,
                num_furniture: int = 0, num_dining_tables: int = 0,
                rooms: list | None = None, additional: str | None = None) -> str:
    rooms_txt = ", ".join(rooms) if rooms else "not specified"
    return (
        f"Plan home interior purchases in India with a total budget of Rs {total_budget:.2f}.\n"
        f"Requirements: {num_lights} lights, {num_fans} ceiling fans, {num_furniture} furniture pieces, "
        f"{num_dining_tables} dining tables.\nRooms: {rooms_txt}.\n"
        f"Additional requirements: {additional or 'None'}.\n"
        f"Balance functionality, style and price (think IKEA, Amazon.in, Flipkart, Havells, Crompton).\n"
        + _RULES + _HOME_SCHEMA
    )


def party_prompt(total_budget: float, num_guests: int, party_type: str,
                 venue_type: str | None = None, needs_catering: bool = True,
                 needs_decoration: bool = True, needs_entertainment: bool = True,
                 additional: str | None = None) -> str:
    yn = lambda b: "Yes" if b else "No"
    return (
        f"Plan a {party_type} party in India with a total budget of Rs {total_budget:.2f}.\n"
        f"Guests: {num_guests}. Venue type: {venue_type or 'not specified'}.\n"
        f"Catering: {yn(needs_catering)}; Decoration: {yn(needs_decoration)}; Entertainment: {yn(needs_entertainment)}.\n"
        f"Additional requirements: {additional or 'None'}.\n"
        f"Split the budget proportionally across the needed categories plus a small contingency "
        f"(think Swiggy, Zomato, OYO, BookMyShow, Amazon.in).\n"
        + _RULES + _PARTY_SCHEMA
    )


def jewelry_prompt(total_budget: float, occasion: str, preferences: str | None = None,
                   has_image: bool = False) -> str:
    img_txt = (
        "An outfit image is attached: first analyse its colours, style and formality, then suggest "
        "jewelry that complements it.\n" if has_image else ""
    )
    return (
        f"Recommend jewelry in India for the occasion '{occasion}' with a total budget of Rs {total_budget:.2f}.\n"
        f"Style preferences: {preferences or 'not specified'}.\n{img_txt}"
        f"(think Amazon.in, Flipkart, BlueStone, Tanishq, CaratLane, Melorra).\n"
        + _RULES + _JEWELRY_SCHEMA
    )
