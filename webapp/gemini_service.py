"""
Gemini AI service — analyses raw KoboToolbox data and produces
a structured CBO profile matching the one-pager template.

Includes a local fallback that computes metrics directly from the data
when the Gemini API is unavailable (rate limit, quota, etc.).
"""
import json
import re
import time
from collections import Counter
from datetime import datetime
from google import genai
from google.genai import types
from flask import current_app

CLASSIFICATION_ALIASES = {
        "tools": ["tool", "tools", "equipment", "rental", "rentals", "tool-sharing"],
        "education": ["education", "school", "schools", "student", "students", "learning", "tutoring"],
        "healthcare": ["healthcare", "health", "clinic", "medical", "wellness", "patients"],
        "agriculture": ["agriculture", "agricultural", "farm", "farms", "farming", "farmer", "farmers", "seeds"],
        "water": ["water", "sanitation", "hygiene", "clean water"],
        "livelihood": ["livelihood", "income", "enterprise", "jobs", "employment"],
        "environment": ["environment", "climate", "conservation", "tree", "trees", "sustainability"],
        "community": ["community", "women", "youth", "social", "cooperative"],
}

MARKETPLACE_QUERY_SCHEMA = """
{
    "search_mode": "semantic or keyword",
    "normalized_query": "string",
    "structured_filters": {
        "text_terms": ["array of short keyword strings"],
        "classification": "one of tools|education|healthcare|agriculture|water|livelihood|environment|community or empty string",
        "badge": "gold|silver|bronze or empty string",
        "min_score": "integer 0-100 or null",
        "max_score": "integer 0-100 or null",
        "min_revenue": "number in Kenyan shillings or null",
        "min_rating": "number 0-10 or null"
    },
    "qualitative_preferences": {
        "feedback_sentiment": "very_positive|positive|neutral|any",
        "mission_alignment_terms": ["array of short phrases"],
        "prioritize": ["array chosen from community_feedback, impact_score, revenue, growth, data_quality, mission_alignment"]
    },
    "recommended_sort": "ai_match|score_desc|revenue_desc|growth_desc|badge|name",
    "explanation": "one-sentence explanation of how the request was interpreted"
}
"""

MARKETPLACE_QUERY_SYSTEM_PROMPT = f"""You turn natural-language marketplace searches into structured filters.

Your job is to:
1. Extract the explicit numeric and categorical constraints.
2. Preserve softer qualitative preferences for later ranking.
3. Keep the result conservative. Only infer filters that are clearly requested.
4. Return ONLY valid JSON matching this schema:

{MARKETPLACE_QUERY_SCHEMA}

Rules:
- Use Kenyan shillings for min_revenue.
- Use min_rating only for community ratings on a 0-10 scale.
- If the user says \"7 or higher\" or \"at least 7\", extract min_rating or min_score accordingly.
- If the request emphasizes reviews, testimonials, trust, positive feedback, or community sentiment, set feedback_sentiment and prioritize community_feedback.
- If the request is broad and open-ended, recommended_sort should be ai_match.
- explanation must be concise and concrete.
"""

MARKETPLACE_RANKING_SCHEMA = """
{
    "summary": "string summarizing why the top matches fit",
    "matches": [
        {
            "slug": "candidate slug",
            "score": "integer 0-100",
            "reasons": ["2-3 short bullet-style reasons"],
            "qualitative_rationale": "one short sentence focused on reviews, focus areas, or mission fit"
        }
    ]
}
"""

MARKETPLACE_RANKING_SYSTEM_PROMPT = f"""You rank marketplace CBO candidates against a user's natural-language search.

You will receive:
- the original user query
- the extracted structured search plan
- a shortlist of candidate summaries

Your task:
1. Prefer candidates that satisfy the hard filters.
2. Use qualitative evidence from community feedback quotes, ratings, focus areas, and tagline text to separate similar candidates.
3. Return ONLY valid JSON matching this schema:

{MARKETPLACE_RANKING_SCHEMA}

Rules:
- Scores must be well-spread across the 0-100 range.
- Do not invent evidence. Use only the candidate summary provided.
- Qualitative rationale should mention reviews or mission alignment when relevant.
"""

STOPWORDS = {
        "a", "an", "and", "are", "as", "at", "be", "best", "by", "cbo", "cbos", "community",
        "for", "from", "has", "have", "higher", "in", "is", "looking", "me", "need", "of", "or",
        "over", "show", "that", "the", "their", "them", "these", "those", "to", "very", "with"
}

POSITIVE_FEEDBACK_WORDS = {
        "good", "great", "excellent", "positive", "helpful", "trusted", "reliable", "improved",
        "amazing", "strong", "supportive", "valuable", "effective", "love", "better", "impactful"
}

NEGATIVE_FEEDBACK_WORDS = {
        "bad", "poor", "negative", "late", "unreliable", "weak", "difficult", "worse", "problem",
        "frustrating", "slow", "inconsistent", "harmful"
}

# ── The profile schema we want Gemini to fill ─────────────────────
PROFILE_SCHEMA = """
{
  "name": "string – CBO name",
  "tagline": "string – short mission statement (≤15 words)",
  "location": "string – County / Region",
  "org_type": "string – e.g. Community-Based Organisation (CBO)",
  "founded_year": "string – year founded or best estimate",
  "focus_areas": "string – comma-separated focus areas",

  "leadership": {
    "chairperson": "string",
    "program_director": "string",
    "finance_lead": "string"
  },
  "governance_note": "string – 1-2 sentence description of governance model",

  "quantified_impact": [
    {
      "icon_hint": "string – one of: clock, farm, chart-up, people, tools, money",
      "metric_value": "string – the number/stat, e.g. '1,500+'",
      "metric_unit": "string – e.g. 'hours'",
      "description": "string – one-line explanation",
      "details": {
        "raw_data": "string – operational detail (e.g. 'Based on 30 rentals across 13 borrowers')",
        "methodology": "string – how this was calculated",
        "breakdown": ["array of 2-4 bullet points with specific breakdowns"]
      }
    }
  ],

  "financial_data": {
    "total_revenue": "string – total rental fees collected (e.g. 'KSh 15,000')",
    "avg_rental_fee": "string – average fee per rental",
    "maintenance_costs": "string – estimated tool maintenance/repair costs",
    "damage_fees_collected": "string – fees from damage charges",
    "operational_model": "string – brief description of pricing structure"
  },

  "operational_metrics": {
    "total_rentals": "number",
    "unique_borrowers": "number",
    "tools_in_inventory": "number",
    "avg_rental_duration_days": "number",
    "on_time_return_rate": "string – percentage",
    "most_popular_tool": "string",
    "busiest_rental_period": "string – e.g. 'January 2026'",
    "maintenance_compliance": "string – percentage of tools returned in good condition"
  },

  "flagship_project": {
    "title": "string – project name",
    "summary": "string – 2-3 sentence description",
    "stats": [
      {"label": "string", "value": "string"}
    ]
  },

  "success_story": {
    "quote": "string – farmer / beneficiary quote",
    "attribution": "string – name & location"
  },

  "join_us": "string – 2-3 sentence call-to-action for funders",

  "classifications": ["array of strings – all sector tags that apply, from: tools, education, healthcare, agriculture, water, livelihood, environment, community"],

  "social_impact_score": "integer 0-100 – rigorous score based on: breadth of reach (25pts), depth of impact per beneficiary (25pts), sustainability indicators (25pts), data quality and transparency (25pts). Be realistic and differentiated.",

  "social_impact_score_rationale": "string – 2-3 sentences explaining the score breakdown"
}
"""

SYSTEM_PROMPT = f"""You are an expert social-impact analyst.
You will receive raw operational data from a Community-Based Organisation
(exported from KoboToolbox). The data will indicate what TYPE of CBO this is
through the cbo_identifier field (e.g., "tools", "education", "healthcare", 
"agriculture", "water").

Your job is to:

1. IDENTIFY the CBO type from the cbo_identifier field in the data
2. Analyze the data SPECIFICALLY for that type of operation:
   - "tools" = tool-sharing/rental program for smallholder farmers
   - "education" = tutoring, learning materials, educational access
   - "healthcare" = medical supplies, health services, wellness programs
   - "agriculture" = seeds, fertilizer, farming equipment, training
   - "water" = clean water access, sanitation, hygiene programs

3. Compute real metrics relevant to THAT specific type of CBO operation
4. Use appropriate terminology and impact metrics for the CBO type
5. Return ONLY valid JSON matching this schema (no markdown, no commentary):

{PROFILE_SCHEMA}

Important guidelines:
- ALWAYS check the cbo_identifier to understand what type of data you're analyzing
- Use sector-appropriate language (e.g., "students" for education, "patients" for healthcare)
- Calculate metrics relevant to the sector:
  * Tools: labour-hours saved, acres cultivated, farmers helped
  * Education: learning hours provided, students served, materials distributed
  * Healthcare: patients served, treatments provided, lives impacted
  * Agriculture: farmers trained, seeds distributed, yields improved
  * Water: households served, liters provided, sanitation access
- For financial_data: estimate fees based on the sector and local context
- Include operational_metrics with sector-specific KPIs
- For the success story, craft a realistic quote from a beneficiary
- All numbers must be derivable from or reasonably estimated from the data
- In details.breakdown for each impact metric, provide 2-4 specific bullet points
- For classifications: include ALL sectors this CBO operates in (can be multiple)
- For social_impact_score: score rigorously 0-100 using ALL four dimensions (breadth of reach 25pts,
  depth of impact per beneficiary 25pts, sustainability indicators 25pts, data quality/transparency 25pts).
  CRITICAL: scores MUST be spread across the full range. Do NOT cluster around 70-75.
  Guidance: <50 submissions or <10 beneficiaries = 20-35. 50-150 submissions = 35-55.
  150-300 submissions = 50-70. 300-500 submissions = 65-82. 500+ with strong outcomes = 82-95.
  A CBO with weak return rates, sparse data, or narrow reach should score in the 20-45 range.
  Be ruthlessly honest — most CBOs should score between 30 and 70, not 70-80.
  Be realistic — not everyone can score 80+.
"""


def _parse_json_response(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    return json.loads(cleaned.strip())


def _extract_money_amount(query: str) -> float | None:
    patterns = [
        r'(?:ksh|kes|shillings?|shs?)\s*([\d,]+(?:\.\d+)?)\s*([km])?',
        r'([\d,]+(?:\.\d+)?)\s*([km])?\s*(?:ksh|kes|shillings?|shs?)',
        r'over\s+([\d,]+(?:\.\d+)?)\s*([km])?',
        r'at\s+least\s+([\d,]+(?:\.\d+)?)\s*([km])?',
        r'more\s+than\s+([\d,]+(?:\.\d+)?)\s*([km])?',
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if not match:
            continue
        number = float(match.group(1).replace(',', ''))
        suffix = (match.group(2) or '').lower()
        if suffix == 'k':
            number *= 1_000
        elif suffix == 'm':
            number *= 1_000_000
        return number
    return None


def _extract_threshold(query: str, label_patterns: list[str], scale_max: int) -> int | None:
    joined = '(?:' + '|'.join(label_patterns) + ')'
    patterns = [
        rf'{joined}[^\d]{{0,20}}(\d{{1,3}})(?:\s*(?:\+|or higher|and above))?',
        rf'(\d{{1,3}})(?:\s*(?:\+|or higher|and above))[^\d]{{0,20}}{joined}',
        rf'at least\s*(\d{{1,3}})[^\d]{{0,20}}{joined}',
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            value = int(match.group(1))
            return max(0, min(scale_max, value))
    return None


def _extract_text_terms(query: str) -> list[str]:
    tokens = re.findall(r'[a-zA-Z]{3,}', query.lower())
    ordered = []
    for token in tokens:
        if token in STOPWORDS or token in ordered:
            continue
        ordered.append(token)
    return ordered[:8]


def _detect_classification(query: str) -> str:
    query_lower = query.lower()
    for classification, aliases in CLASSIFICATION_ALIASES.items():
        if any(alias in query_lower for alias in aliases):
            return classification
    return ''


def _feedback_preference(query: str) -> str:
    query_lower = query.lower()
    if any(phrase in query_lower for phrase in ["very positive", "extremely positive", "glowing reviews", "excellent feedback"]):
        return "very_positive"
    if any(phrase in query_lower for phrase in ["positive feedback", "good reviews", "strong reviews", "well reviewed"]):
        return "positive"
    if any(phrase in query_lower for phrase in ["neutral", "mixed feedback"]):
        return "neutral"
    return "any"


def _fallback_marketplace_query_plan(query: str) -> dict:
    min_rating = _extract_threshold(query, [r'ratings?', r'reviews?', r'community ratings?'], 10)
    min_score = _extract_threshold(query, [r'impact score', r'social impact'], 100)
    min_revenue = _extract_money_amount(query)
    classification = _detect_classification(query)
    feedback_sentiment = _feedback_preference(query)
    prioritize = []
    query_lower = query.lower()

    if feedback_sentiment != 'any' or any(word in query_lower for word in ['review', 'reviews', 'feedback', 'community']):
        prioritize.append('community_feedback')
    if any(word in query_lower for word in ['impact', 'outcomes', 'benefit']):
        prioritize.append('impact_score')
    if min_revenue or any(word in query_lower for word in ['revenue', 'income', 'earnings']):
        prioritize.append('revenue')
    if any(word in query_lower for word in ['growth', 'growing', 'fastest']):
        prioritize.append('growth')
    if any(word in query_lower for word in ['data quality', 'trusted data', 'verified']):
        prioritize.append('data_quality')
    if classification or any(word in query_lower for word in ['focus', 'mission', 'serves']):
        prioritize.append('mission_alignment')

    if not prioritize:
        prioritize = ['mission_alignment', 'community_feedback']

    explanation_bits = []
    if classification:
        explanation_bits.append(f"sector {classification}")
    if min_revenue is not None:
        explanation_bits.append(f"revenue >= KSh {min_revenue:,.0f}")
    if min_rating is not None:
        explanation_bits.append(f"community rating >= {min_rating}/10")
    if min_score is not None:
        explanation_bits.append(f"impact score >= {min_score}")
    if feedback_sentiment != 'any':
        explanation_bits.append(f"{feedback_sentiment.replace('_', ' ')} feedback preference")

    return {
        'search_mode': 'semantic' if len(query.split()) >= 4 else 'keyword',
        'normalized_query': re.sub(r'\s+', ' ', query).strip(),
        'structured_filters': {
            'text_terms': _extract_text_terms(query),
            'classification': classification,
            'badge': '',
            'min_score': min_score,
            'max_score': None,
            'min_revenue': min_revenue,
            'min_rating': min_rating,
        },
        'qualitative_preferences': {
            'feedback_sentiment': feedback_sentiment,
            'mission_alignment_terms': _extract_text_terms(query)[:4],
            'prioritize': prioritize,
        },
        'recommended_sort': 'ai_match',
        'explanation': 'Interpreted request using local semantic rules' + (f": {', '.join(explanation_bits)}." if explanation_bits else '.'),
    }


def interpret_marketplace_query(query: str) -> dict:
    query = (query or '').strip()
    if not query:
        return _fallback_marketplace_query_plan('')

    api_key = current_app.config['GEMINI_API_KEY']
    if not api_key:
        return _fallback_marketplace_query_plan(query)

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                MARKETPLACE_QUERY_SYSTEM_PROMPT,
                f'User query: {query}',
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type='application/json',
            ),
        )
        plan = _parse_json_response(response.text)
        if not isinstance(plan, dict):
            raise ValueError('Gemini marketplace query plan was not a JSON object')
        plan.setdefault('structured_filters', {})
        plan.setdefault('qualitative_preferences', {})
        plan.setdefault('recommended_sort', 'ai_match')
        plan.setdefault('normalized_query', query)
        plan.setdefault('explanation', 'Interpreted request with Gemini')
        return plan
    except Exception as exc:
        current_app.logger.warning('Gemini marketplace query interpretation failed: %s', exc)
        return _fallback_marketplace_query_plan(query)


def _quote_sentiment_score(quotes: list[str]) -> float:
    if not quotes:
        return 0.0
    score = 0.0
    for quote in quotes:
        text = (quote or '').lower()
        score += sum(1 for word in POSITIVE_FEEDBACK_WORDS if word in text) * 1.5
        score -= sum(1 for word in NEGATIVE_FEEDBACK_WORDS if word in text) * 2.0
    return max(-10.0, min(10.0, score))


def _local_marketplace_ranking(query: str, candidates: list[dict], search_plan: dict | None = None) -> dict:
    search_plan = search_plan or _fallback_marketplace_query_plan(query)
    structured = search_plan.get('structured_filters', {})
    qualitative = search_plan.get('qualitative_preferences', {})
    target_class = (structured.get('classification') or '').lower()
    text_terms = [term.lower() for term in structured.get('text_terms', []) if term]
    feedback_pref = qualitative.get('feedback_sentiment', 'any')
    mission_terms = [term.lower() for term in qualitative.get('mission_alignment_terms', []) if term]
    priorities = qualitative.get('prioritize', [])
    target_min_revenue = float(structured.get('min_revenue') or 0)
    target_min_score = int(structured.get('min_score') or 0)
    target_min_rating = float(structured.get('min_rating') or 0)

    matches = []
    for candidate in candidates:
        reasons = []
        score = 0.0
        searchable = ' '.join([
            candidate.get('name', ''),
            candidate.get('location', ''),
            candidate.get('focus_areas', ''),
            candidate.get('tagline', ''),
            ' '.join(candidate.get('classifications', [])),
        ]).lower()
        term_hits = sum(1 for term in text_terms if term in searchable)
        mission_hits = sum(1 for term in mission_terms if term in searchable)
        review_sentiment = _quote_sentiment_score(candidate.get('recent_quotes', []))
        avg_rating = candidate.get('avg_rating')
        impact_score = candidate.get('score') or 0
        total_revenue = float(candidate.get('total_revenue') or 0)
        growth = float(candidate.get('revenue_growth') or 0)
        classifications = [value.lower() for value in candidate.get('classifications', [])]
        badge = (candidate.get('badge') or '').lower()

        if target_class and target_class in classifications:
            score += 18
            reasons.append(f"Matches requested {target_class} sector")
        if term_hits:
            score += min(20, term_hits * 5)
            reasons.append(f"Aligns with {term_hits} search term{'s' if term_hits != 1 else ''}")
        if mission_hits:
            score += min(12, mission_hits * 4)
            reasons.append('Mission and focus areas fit the request')
        if avg_rating is not None:
            rating_bonus = avg_rating * 2.4
            score += rating_bonus
            if target_min_rating and avg_rating >= target_min_rating:
                reasons.append(f"Community rating is {avg_rating}/10")
        if impact_score:
            score += impact_score * 0.18
            if target_min_score and impact_score >= target_min_score:
                reasons.append(f"Impact score reaches {impact_score}/100")
        if total_revenue:
            if target_min_revenue:
                revenue_ratio = min(total_revenue / max(target_min_revenue, 1), 2.0)
                score += revenue_ratio * 8
                if total_revenue >= target_min_revenue:
                    reasons.append(f"Revenue clears KSh {target_min_revenue:,.0f}")
            else:
                score += min(10, total_revenue / 50_000)
        if 'community_feedback' in priorities:
            score += max(0, review_sentiment) * 1.8
            if candidate.get('responses'):
                reasons.append(f"Backed by {candidate.get('responses')} community response{'s' if candidate.get('responses') != 1 else ''}")
        if feedback_pref == 'very_positive' and avg_rating is not None:
            score += 10 if avg_rating >= 8.5 else 2
        elif feedback_pref == 'positive' and avg_rating is not None:
            score += 8 if avg_rating >= 7 else 1
        elif feedback_pref == 'neutral' and avg_rating is not None:
            score += 4 if 5 <= avg_rating <= 7.5 else 0
        if 'growth' in priorities and growth > 0:
            score += min(10, growth / 5)
        if 'data_quality' in priorities:
            score += {'gold': 9, 'silver': 5, 'bronze': 2}.get(badge, 0)

        matches.append({
            'slug': candidate.get('slug'),
            'score': int(max(0, min(100, round(score)))),
            'reasons': reasons[:3],
            'qualitative_rationale': 'Strong qualitative fit based on mission, ratings, and recent feedback.' if review_sentiment >= 4 else 'Best fit is driven more by structured metrics than qualitative evidence.',
        })

    matches.sort(key=lambda item: item['score'], reverse=True)
    return {
        'summary': 'Ranked with local heuristics using ratings, revenue, mission alignment, and recent community feedback.',
        'matches': matches,
    }


def rank_marketplace_candidates(query: str, candidates: list[dict], search_plan: dict | None = None) -> dict:
    if not candidates:
        return {'summary': '', 'matches': []}

    local_ranking = _local_marketplace_ranking(query, candidates, search_plan)
    local_lookup = {match['slug']: match for match in local_ranking['matches']}
    api_key = current_app.config['GEMINI_API_KEY']
    if not api_key:
        return local_ranking

    shortlist = []
    for match in local_ranking['matches'][:20]:
        candidate = next((item for item in candidates if item.get('slug') == match['slug']), None)
        if candidate:
            shortlist.append(candidate)

    if not shortlist:
        return local_ranking

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                MARKETPLACE_RANKING_SYSTEM_PROMPT,
                json.dumps({
                    'query': query,
                    'search_plan': search_plan or {},
                    'candidates': shortlist,
                }, indent=2, default=str),
            ],
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type='application/json',
            ),
        )
        gemini_ranking = _parse_json_response(response.text)
        gemini_matches = gemini_ranking.get('matches', []) if isinstance(gemini_ranking, dict) else []
        combined = []
        seen = set()

        for match in gemini_matches:
            slug = match.get('slug')
            if not slug or slug not in local_lookup:
                continue
            local_score = local_lookup[slug]['score']
            combined_score = int(round((0.65 * int(match.get('score', 0))) + (0.35 * local_score)))
            combined.append({
                'slug': slug,
                'score': max(0, min(100, combined_score)),
                'reasons': (match.get('reasons') or local_lookup[slug].get('reasons') or [])[:3],
                'qualitative_rationale': match.get('qualitative_rationale') or local_lookup[slug].get('qualitative_rationale', ''),
            })
            seen.add(slug)

        for local_match in local_ranking['matches']:
            if local_match['slug'] in seen:
                continue
            combined.append(local_match)

        combined.sort(key=lambda item: item['score'], reverse=True)
        return {
            'summary': gemini_ranking.get('summary') or local_ranking['summary'],
            'matches': combined,
        }
    except Exception as exc:
        current_app.logger.warning('Gemini marketplace ranking failed: %s', exc)
        return local_ranking


def compute_data_quality_badge(raw_submissions: list[dict]) -> str:
    """
    Compute a bronze/silver/gold data quality badge based on:
    - Volume:   number of records (40 pts)  — gold requires 200+, silver 80+
    - Coverage: field completeness per record (40 pts)
    - Span:     months of history (20 pts)

    Hard minimums (gates):
      Gold   → ≥200 records AND ≥8 months AND avg coverage ≥60%
      Silver → ≥80 records  AND ≥4 months AND avg coverage ≥40%
      Bronze → everything else
    """
    if not raw_submissions:
        return "bronze"

    KEY_FIELDS = [
        "date_loaned", "date_returned", "tool_name", "borrower_name",
        "borrower_signature", "condition_upon_return", "time_loaned",
        "time_returned", "damage_charged", "return_notes", "serial_number", "quantity"
    ]

    total = len(raw_submissions)

    # Field coverage per record
    field_scores = []
    for rec in raw_submissions:
        filled = sum(1 for f in KEY_FIELDS if rec.get(f) and str(rec.get(f)).strip() not in ('', '—'))
        field_scores.append(filled / len(KEY_FIELDS))
    avg_coverage = sum(field_scores) / len(field_scores)   # 0.0 – 1.0

    # Date span in months
    dates = []
    for rec in raw_submissions:
        try:
            from datetime import datetime as _dt
            dates.append(_dt.strptime(rec.get('date_loaned', ''), '%Y-%m-%d'))
        except Exception:
            pass
    months_span = 0
    if len(dates) >= 2:
        months_span = (max(dates) - min(dates)).days / 30

    # Continuous score (for tie-breaking, not for badge thresholds)
    volume_score   = min(total / 300, 1.0)          # full at 300+ records
    span_score     = min(months_span / 12, 1.0)     # full at 12+ months
    score = (volume_score * 40) + (avg_coverage * 40) + (span_score * 20)

    # ── Hard-gate thresholds ─────────────────────────────────────
    # Gold: must clear ALL three gates AND score ≥ 75
    if total >= 200 and months_span >= 8 and avg_coverage >= 0.60 and score >= 75:
        return "gold"
    # Silver: must clear ALL three gates AND score ≥ 45
    if total >= 80 and months_span >= 4 and avg_coverage >= 0.40 and score >= 45:
        return "silver"
    return "bronze"


def _compute_local_profile(raw_submissions: list[dict], cbo_name: str) -> dict:
    """
    Fallback: compute CBO profile metrics directly from raw data
    without calling Gemini. Produces the same JSON schema.
    Context-aware based on cbo_identifier field.
    """
    total = len(raw_submissions)
    borrowers = set()
    tools = set()
    conditions = Counter()
    total_days = 0
    total_qty = 0
    damage_count = 0
    good_return = 0
    
    # Detect CBO type from data
    cbo_type = "tools"  # default
    if raw_submissions:
        cbo_type = raw_submissions[0].get('cbo_identifier', 'tools')
    
    # Type-specific configurations
    if cbo_type == "education":
        impact_unit = "learning hours"
        beneficiary_term = "students"
        item_term = "resources"
        multiplier = 15  # hours per activity
    elif cbo_type == "healthcare":
        impact_unit = "patients served"
        beneficiary_term = "patients"
        item_term = "services"
        multiplier = 1  # one service per activity
    elif cbo_type == "agriculture":
        impact_unit = "farmers trained"
        beneficiary_term = "farmers"
        item_term = "inputs"
        multiplier = 5  # kg seeds or similar per activity
    elif cbo_type == "water":
        impact_unit = "households served"
        beneficiary_term = "households"
        item_term = "interventions"
        multiplier = 20  # liters per intervention
    else:  # tools
        impact_unit = "labour-hours saved"
        beneficiary_term = "farmers"
        item_term = "tools"
        multiplier = 10  # hours saved per day

    for r in raw_submissions:
        name = r.get('borrower_name', '') or ''
        if name:
            borrowers.add(name)
        tool = r.get('tool_name', '') or ''
        if tool:
            tools.add(tool)
        cond = r.get('condition_upon_return', '') or ''
        conditions[cond] += 1
        if 'good' in cond.lower() or 'excellent' in cond.lower():
            good_return += 1

        dmg = r.get('damage_charged', '') or ''
        if dmg and dmg not in ('—', 'No charge', ''):
            damage_count += 1

        qty = 1
        try:
            qty = int(r.get('quantity', 1))
        except (ValueError, TypeError):
            pass
        total_qty += qty

        # Calculate days borrowed
        try:
            d1 = datetime.strptime(r.get('date_loaned', ''), '%Y-%m-%d')
            d2 = datetime.strptime(r.get('date_returned', ''), '%Y-%m-%d')
            total_days += max((d2 - d1).days, 1)
        except (ValueError, TypeError):
            total_days += 5  # default estimate

    avg_days = round(total_days / max(total, 1), 1)
    return_rate = round(100 * good_return / max(total, 1))
    
    # Calculate primary impact metric based on CBO type
    primary_impact_value = total_days * multiplier
    if cbo_type == "agriculture":
        acres = total_qty * 3  # 3 acres per input distribution
    elif cbo_type == "tools":
        acres = total_qty * 3
    else:
        acres = 0
    
    # Estimate finances (type-specific)
    if cbo_type == "education":
        avg_fee = 50  # KSh per tutoring session
    elif cbo_type == "healthcare":
        avg_fee = 100  # KSh per service
    elif cbo_type == "agriculture":
        avg_fee = 200  # KSh per input package
    elif cbo_type == "water":
        avg_fee = 75  # KSh per water access
    else:
        avg_fee = 120  # KSh per tool rental
        
    total_revenue = total * avg_fee
    dmg_fees = damage_count * 350  # avg damage/maintenance fee
    maintenance = int(total_revenue * 0.08)
    
    # Find most popular item
    tool_counts = Counter()
    farm_keywords = {'wheelbarrow', 'hoe', 'panga', 'spade', 'fork', 'saw', 'hose', 'rake'}
    farm_rentals = 0
    for r in raw_submissions:
        tool = r.get('tool_name', '')
        if tool:
            tool_counts[tool] += 1
            if any(k in tool.lower() for k in farm_keywords):
                farm_rentals += 1
    most_popular = tool_counts.most_common(1)[0][0] if tool_counts else 'N/A'
    
    # Type-specific taglines and descriptions
    if cbo_type == "education":
        tagline = "Empowering Communities Through Accessible Education"
        org_focus = "Education access, tutoring, learning materials, youth development"
    elif cbo_type == "healthcare":
        tagline = "Building Healthier Communities Through Quality Care"
        org_focus = "Primary healthcare, medical supplies, wellness programs, maternal health"
    elif cbo_type == "agriculture":
        tagline = "Cultivating Food Security Through Sustainable Farming"
        org_focus = "Sustainable farming, seeds distribution, agricultural training, food security"
    elif cbo_type == "water":
        tagline = "Ensuring Clean Water Access for All"
        org_focus = "Clean water access, sanitation facilities, hygiene education, community health"
    else:
        tagline = "Empowering Farmers Through Sustainable Tool Access"
        org_focus = "Rural livelihood, subsistence agriculture, tool sharing"

    return {
        "name": cbo_name,
        "tagline": tagline,
        "location": "Rural Kenya",
        "org_type": "Community-Based Organisation (CBO)",
        "founded_year": "2023",
        "focus_areas": org_focus,
        "leadership": {
            "chairperson": list(borrowers)[0] if borrowers else "Community Leader",
            "program_director": list(borrowers)[1] if len(borrowers) > 1 else "Program Manager",
            "finance_lead": list(borrowers)[2] if len(borrowers) > 2 else "Finance Officer"
        },
        "governance_note": f"Governed by a dedicated board of community members committed to ensuring {beneficiary_term} have access to essential resources.",
        "quantified_impact": [
            {
                "icon_hint": "people" if cbo_type in ["education", "healthcare", "water"] else "clock",
                "metric_value": f"{primary_impact_value:,}+",
                "metric_unit": impact_unit.split()[-1],
                "description": f"Total {impact_unit} provided to {len(borrowers)} {beneficiary_term}",
                "details": {
                    "raw_data": f"Based on {total} activities across {len(borrowers)} {beneficiary_term} over {total_days} days",
                    "methodology": f"Calculated at {multiplier} {impact_unit} per activity day",
                    "breakdown": [
                        f"Average activity duration: {avg_days} days",
                        f"Total {beneficiary_term} served: {len(borrowers)}",
                        f"Impact per activity: ~{int(primary_impact_value/max(total,1))} {impact_unit.split()[-1]}"
                    ]
                }
            },
            {
                "icon_hint": "farm",
                "metric_value": f">{acres}",
                "metric_unit": "acres",
                "description": "Cultivated using rented farm equipment",
                "details": {
                    "raw_data": f"{farm_rentals} farm tool rentals (Wheelbarrows, Garden Hoses, Saws, etc.)",
                    "methodology": "Estimates 3 acres cultivated per farm-tool rental",
                    "breakdown": [
                        f"Farm equipment rentals: {farm_rentals} transactions",
                        f"Average land serviced per rental: 3 acres",
                        f"Proportion of farm vs. non-farm tools: {int(100*farm_rentals/max(total,1))}% farm equipment"
                    ]
                }
            },
            {
                "icon_hint": "tools",
                "metric_value": str(len(tools)),
                "metric_unit": "tools managed",
                "description": f"Unique tools available across {total_qty} total items",
                "details": {
                    "raw_data": f"{len(tools)} unique tool types, {total_qty} total inventory items",
                    "methodology": "Count of distinct tools appearing in rental records",
                    "breakdown": [
                        f"Most popular tool: {most_popular} ({tool_counts.get(most_popular, 0)} rentals)",
                        f"Average quantity per tool type: {round(total_qty/max(len(tools),1), 1)} units",
                        f"Tool utilization rate: {int(100*total/max(total_qty,1))}%"
                    ]
                }
            },
            {
                "icon_hint": "people",
                "metric_value": f"{len(borrowers)}+",
                "metric_unit": "active borrowers",
                "description": "Unique community members served",
                "details": {
                    "raw_data": f"{len(borrowers)} unique borrowers across {total} rentals",
                    "methodology": "Count of distinct borrower names in transaction records",
                    "breakdown": [
                        f"Average rentals per borrower: {round(total/max(len(borrowers),1), 1)} times",
                        f"Repeat borrower rate: {int(100*(1 - len(borrowers)/max(total,1)))}%",
                        f"Community reach growing steadily"
                    ]
                }
            },
            {
                "icon_hint": "check",
                "metric_value": f"{return_rate}%",
                "metric_unit": "return rate",
                "description": "Tools returned in good or excellent condition",
                "details": {
                    "raw_data": f"{good_return} out of {total} rentals returned in good/excellent condition",
                    "methodology": "Based on condition_upon_return field in transaction data",
                    "breakdown": [
                        f"Good/Excellent returns: {good_return} rentals",
                        f"Damage incidents requiring charges: {damage_count} cases",
                        f"Community trust and responsibility rate: {return_rate}%"
                    ]
                }
            },
            {
                "icon_hint": "chart-up",
                "metric_value": f"{avg_days}",
                "metric_unit": "avg days",
                "description": "Average rental period per transaction",
                "details": {
                    "raw_data": f"{total_days} total rental days across {total} transactions",
                    "methodology": "Calculated from date_loaned to date_returned for each rental",
                    "breakdown": [
                        f"Shortest rental: 1 day (quick projects)",
                        f"Average duration: {avg_days} days",
                        f"Flexible rental terms support diverse community needs"
                    ]
                }
            },
            {
                "icon_hint": "money",
                "metric_value": f"KSh {total_revenue:,}",
                "metric_unit": "revenue",
                "description": "Estimated total rental fees collected",
                "details": {
                    "raw_data": f"{total} rentals at ~KSh {avg_fee} average fee",
                    "methodology": "Estimated based on typical CBO rental pricing (KSh 50-200 per tool)",
                    "breakdown": [
                        f"Rental fee revenue: KSh {total_revenue:,}",
                        f"Damage fees collected: KSh {dmg_fees:,}",
                        f"Estimated maintenance costs: KSh {maintenance:,}"
                    ]
                }
            }
        ],
        "financial_data": {
            "total_revenue": f"KSh {total_revenue:,}",
            "avg_rental_fee": f"KSh {avg_fee}",
            "maintenance_costs": f"KSh {maintenance:,}",
            "damage_fees_collected": f"KSh {dmg_fees:,}",
            "operational_model": f"Affordable rental fees (KSh 50-200 per tool) with damage deposits. {return_rate}% of tools returned in good condition."
        },
        "operational_metrics": {
            "total_rentals": total,
            "unique_borrowers": len(borrowers),
            "tools_in_inventory": len(tools),
            "avg_rental_duration_days": avg_days,
            "on_time_return_rate": f"{return_rate}%",
            "most_popular_tool": most_popular,
            "busiest_rental_period": "Recent months",
            "maintenance_compliance": f"{return_rate}%"
        },
        "flagship_project": {
            "title": "Community Tool Rental Program",
            "summary": f"Since 2023, the program has provided affordable tool access for {len(borrowers)} smallholder farmers through a transparent, community-managed rental system.",
            "stats": [
                {"label": "Total Tools Managed", "value": f"{len(tools)}"},
                {"label": "Total Rentals Completed", "value": str(total)},
                {"label": "Active Borrowers", "value": f"{len(borrowers)}+ unique farmers"},
                {"label": "Tool Return Rate", "value": f"{return_rate}%"},
                {"label": "Average Rental Period", "value": f"{avg_days} days"}
            ]
        },
        "success_story": {
            "quote": "Before, I had to borrow tools from neighbors and delay planting. Now, I can rent what I need for just KSh 50 and return it the same day. It's changed how we farm.",
            "attribution": f"Community member — {cbo_name}"
        },
        "join_us": f"We're seeking partnerships with grant-giving organizations, impact investors and agricultural development agencies to expand access to farm tools and empower rural communities. {cbo_name} has demonstrated strong operational discipline with {total} completed rentals and a {return_rate}% return rate.",
        "classifications": [cbo_type],
        "social_impact_score": min(92, max(15,
            # Breadth: unique beneficiaries (0-25pts)
            int(min(len(borrowers), 500) / 500 * 25)
            # Volume: total transactions (0-25pts)
            + int(min(total, 500) / 500 * 25)
            # Quality: return/outcome rate (0-25pts)
            + int(return_rate / 100 * 25)
            # Data: avg field coverage via total_days proxy (0-25pts)
            + int(min(avg_days * 2, 25))
        )),
        "social_impact_score_rationale": f"Score derived from {return_rate}% return rate, {total} total transactions, and {len(borrowers)} unique beneficiaries reached."
    }


def analyse_kobo_data(raw_submissions: list[dict], cbo_name: str = "Community Tool Hub") -> dict:
    """
    Send raw KoboToolbox submissions to Gemini and get back a
    structured CBO profile dict. Falls back to local analysis
    if Gemini is unavailable.
    """
    api_key = current_app.config['GEMINI_API_KEY']
    if not api_key:
        print("  ⚠  No Gemini API key — using local analysis fallback")
        return _compute_local_profile(raw_submissions, cbo_name)

    try:
        client = genai.Client(api_key=api_key)

        # Summarise the data so we don't blow the context window
        data_summary = json.dumps(raw_submissions[:100], indent=2, default=str)

        user_prompt = f"""Here is the raw KoboToolbox tool-rental data for a CBO
called "{cbo_name}". There are {len(raw_submissions)} total submission records.

DATA:
{data_summary}

Analyse this data and return the structured CBO profile JSON."""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                SYSTEM_PROMPT,
                user_prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )

        # Parse the JSON response
        try:
            profile = _parse_json_response(response.text)
        except json.JSONDecodeError:
            profile = _compute_local_profile(raw_submissions, cbo_name)

        return profile

    except Exception as e:
        print(f"  ⚠  Gemini API error ({e}) — using local analysis fallback")
        return _compute_local_profile(raw_submissions, cbo_name)


def compute_growth_metrics(raw_submissions):
    """
    Compute monthly growth metrics from raw KoboToolbox submissions.
    Returns a list of monthly data points for time-series visualization.
    
    Returns:
        [
            {
                "month": "2024-06",
                "rentals": 15,
                "borrowers": 8,
                "revenue": 350,
                "tools_in_use": 12,
                "avg_duration": 4.2
            },
            ...
        ]
    """
    from collections import defaultdict
    from datetime import datetime
    
    # Group submissions by month
    by_month = defaultdict(lambda: {
        "submissions": [],
        "borrowers": set(),
        "tools": set(),
        "revenues": []
    })
    
    for record in raw_submissions:
        # Parse loan date
        date_str = record.get('date_loaned', '')
        if not date_str:
            continue
        
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
            month_key = dt.strftime('%Y-%m')  # e.g., "2024-06"
        except:
            continue
        
        by_month[month_key]["submissions"].append(record)
        by_month[month_key]["borrowers"].add(record.get('borrower_name', 'Unknown'))
        by_month[month_key]["tools"].add(record.get('tool_name', 'Tool'))
        
        # Extract revenue from damage charges
        dmg = record.get('damage_charged', '—')
        if dmg and dmg != '—':
            try:
                # Extract numeric value from strings like "$10 minor damage"
                import re
                matches = re.findall(r'\$?(\d+)', dmg)
                if matches:
                    by_month[month_key]["revenues"].append(int(matches[0]))
            except:
                pass
    
    # Convert to time-series list
    metrics = []
    for month in sorted(by_month.keys()):
        data = by_month[month]
        submissions = data["submissions"]
        
        # Calculate average rental duration
        durations = []
        for rec in submissions:
            try:
                loan_date = datetime.strptime(rec.get('date_loaned', ''), '%Y-%m-%d')
                return_date = datetime.strptime(rec.get('date_returned', ''), '%Y-%m-%d')
                duration = (return_date - loan_date).days
                if duration > 0:
                    durations.append(duration)
            except:
                pass
        
        avg_duration = round(sum(durations) / len(durations), 1) if durations else 0
        
        metrics.append({
            "month": month,
            "rentals": len(submissions),
            "borrowers": len(data["borrowers"]),
            "revenue": sum(data["revenues"]) if data["revenues"] else 0,
            "tools_in_use": len(data["tools"]),
            "avg_duration": avg_duration
        })
    
    return metrics

