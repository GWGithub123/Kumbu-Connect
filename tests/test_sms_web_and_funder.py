import unittest

from webapp.cbo_web_research_service import (
    apply_verification_decision,
    apply_web_research_to_cbo,
    format_web_verification_sms,
    parse_web_verification_reply,
    _is_sparse_profile_value,
    _append_unique_highlight,
)
from webapp.community_feedback import build_community_feedback_signup_body, build_sms_compose_url
from webapp.community_feedback_keywords import get_cbo_keyword
from webapp.funder_personalization_service import prioritize_metric_cards


class FakeCBO:
    def __init__(self, name='Acme Youth Group', sms_keyword='KIWA', slug='acme-youth', cbo_id=4):
        self.name = name
        self.sms_keyword = sms_keyword
        self.slug = slug
        self.id = cbo_id


class WebVerificationTests(unittest.TestCase):
    def setUp(self):
        self.claims = [
            {'id': 1, 'headline': 'Founded in 2008'},
            {'id': 2, 'headline': 'Works with youth in Kisumu'},
            {'id': 3, 'headline': 'Ran a seed program'},
        ]

    def test_yes_accepts_all_claims(self):
        decision = parse_web_verification_reply('YES', self.claims)
        self.assertEqual(decision['decision'], 'accept_all')
        self.assertEqual(decision['verified_ids'], [1, 2, 3])

    def test_no_rejects_all_claims(self):
        decision = parse_web_verification_reply('No', self.claims)
        self.assertEqual(decision['decision'], 'reject_all')

    def test_numbered_correction(self):
        decision = parse_web_verification_reply('2 is wrong', self.claims)
        self.assertEqual(decision['decision'], 'partial')
        self.assertEqual(decision['rejected_ids'], [2])
        self.assertEqual(decision['verified_ids'], [1, 3])

    def test_skip(self):
        decision = parse_web_verification_reply('SKIP', self.claims)
        self.assertEqual(decision['decision'], 'skip')

    def test_apply_partial_decision(self):
        decision = parse_web_verification_reply('2 is wrong', self.claims)
        updated = apply_verification_decision({'claims': self.claims, 'status': 'ready'}, decision)
        verified = {item['id']: item['verified'] for item in updated['claims']}
        self.assertTrue(verified[1])
        self.assertFalse(verified[2])
        self.assertTrue(verified[3])
        self.assertEqual(updated['status'], 'verified')

    def test_verification_sms_stays_short(self):
        message = format_web_verification_sms('Acme Youth Group', {
            'claims': [
                {'id': 1, 'headline': 'Founded in 2008', 'source_title': 'The Star'},
                {'id': 2, 'headline': 'Youth livelihoods in Kisumu', 'source_title': 'Nation'},
            ],
            'summary': 'Local CBO mentioned in two news articles.',
        })
        self.assertIn('YES', message)
        self.assertIn('Acme Youth Group', message)
        self.assertLessEqual(len(message), 1500)


class SmsLinkTests(unittest.TestCase):
    def test_compose_url_prefills_body(self):
        url = build_sms_compose_url('+18336327146', 'KIWA Acme Youth Group')
        self.assertTrue(url.startswith('sms:+18336327146?&body='))
        self.assertIn('KIWA', url)

    def test_signup_body_starts_with_keyword(self):
        cbo = FakeCBO()
        body = build_community_feedback_signup_body(cbo)
        self.assertTrue(body.startswith(get_cbo_keyword(cbo)))
        self.assertIn('Acme Youth Group', body)

    def test_first_token_keyword_still_matches_helper(self):
        cbo = FakeCBO()
        keyword = get_cbo_keyword(cbo)
        first_token = 'KIWA Acme Youth Group'.split()[0]
        self.assertEqual(first_token, keyword)


class FunderLayoutTests(unittest.TestCase):
    def test_metric_cards_prioritize_matching_interests(self):
        cards = [
            {'label': 'Annual Budget', 'value': 'KSh 1M'},
            {'label': 'Farmers Reached', 'value': '400'},
            {'label': 'Avg Session Duration', 'value': '12'},
        ]
        ordered = prioritize_metric_cards(cards, ['agriculture'])
        self.assertEqual(ordered[0]['label'], 'Farmers Reached')


class FunderMatchTests(unittest.TestCase):
    def _funder(self, interests, learned=None, geos=None, themes='', populations=None, about=''):
        import json

        class Profile:
            organization_name = 'Griffin White'
            website = ''
            country_region = 'Kenya'
            about_text = about
            interest_areas_json = json.dumps(interests)
            geographic_focus_json = json.dumps(geos or [])
            grant_size_min = 2000.0
            grant_size_max = 12000.0
            grant_currency = 'KES'
            preference_memory_json = json.dumps({'learned_interests': learned or []})
            interest_brief_json = json.dumps({
                'themes': themes,
                'populations': populations or [],
                'outcomes': [],
                'evidence_needed': '',
            })

        return Profile()

    def _cbo(self, **kwargs):
        class Org:
            name = kwargs.get('name', 'Test CBO')
            focus_areas = kwargs.get('focus_areas', '')
            location = kwargs.get('location', 'Nairobi')
            county_region = kwargs.get('county_region', 'Nairobi County')
            flagship_summary = kwargs.get('flagship_summary', '')
            success_story = kwargs.get('success_story', '')
            classifications_json = kwargs.get('classifications_json', '[]')
            cbo_identifier = kwargs.get('cbo_identifier', 'community')
            social_impact_score = kwargs.get('social_impact_score', 55)
            founded_year = kwargs.get('founded_year', '2024')
            chairperson = kwargs.get('chairperson', '')
            program_director = kwargs.get('program_director', '')
            finance_lead = kwargs.get('finance_lead', '')

        return Org()

    def test_agriculture_funder_does_not_match_education_cbo(self):
        from webapp.funder_personalization_service import score_cbo_for_funder

        funder = self._funder(['agriculture'])
        cbo = self._cbo(
            name='Kibera Homework Club',
            focus_areas='educational access, academic support, digital literacy, youth empowerment',
            classifications_json='["education", "livelihood", "community"]',
            cbo_identifier='education',
            location='Kibera, Nairobi County',
        )
        profile = {
            'tagline': 'Empowering Kibera youth through accessible learning resources',
            'focus_areas': 'educational access, academic support, digital literacy, youth empowerment',
            'classifications': ['education', 'livelihood', 'community'],
            'operational_metric_cards': [
                {'label': 'Students Served', 'value': '5'},
                {'label': 'Total Sessions', 'value': '27'},
            ],
        }
        match = score_cbo_for_funder(cbo, funder, profile)
        self.assertFalse(match['is_match'])
        self.assertNotIn('education', match['overlap'])

    def test_learned_interests_do_not_create_a_match_badge(self):
        from webapp.funder_personalization_service import score_cbo_for_funder

        funder = self._funder(['agriculture'], learned=['education', 'youth', 'livelihood'])
        cbo = self._cbo(
            name='Bright Futures Education CBO',
            focus_areas='Education access, tutoring, learning materials, youth development',
            classifications_json='["education"]',
            cbo_identifier='education',
        )
        match = score_cbo_for_funder(cbo, funder, {
            'tagline': 'Empowering students through accessible learning resources',
            'classifications': ['education'],
        })
        self.assertFalse(match['is_match'])

    def test_agriculture_funder_matches_farm_cbo_with_profile_evidence(self):
        from webapp.funder_personalization_service import score_cbo_for_funder

        funder = self._funder(['agriculture'], themes='smallholder irrigation and farmer cooperatives')
        cbo = self._cbo(
            name='Kakamega Farm Collective',
            focus_areas='Smallholder agriculture, women\'s empowerment, sustainable farming',
            classifications_json='["tools", "agriculture", "livelihood"]',
            cbo_identifier='tools',
            location='Kakamega County, Kenya',
            flagship_summary='Shared farm tools and training for smallholder farmers across Kakamega.',
        )
        match = score_cbo_for_funder(cbo, funder, {
            'tagline': 'Empowering smallholder farmers with accessible tools',
            'focus_areas': 'Smallholder agriculture, women\'s empowerment, sustainable farming',
            'classifications': ['tools', 'agriculture', 'livelihood'],
            'flagship_summary': 'Shared farm tools and training for smallholder farmers across Kakamega.',
            'operational_metric_cards': [
                {'label': 'Farmers', 'value': '9'},
                {'label': 'Acres', 'value': '35.8'},
            ],
        })
        self.assertTrue(match['is_match'])
        self.assertIn('agriculture', match['overlap'])
        self.assertTrue(match['reasons'])
        joined = ' '.join(match['reasons']).lower()
        self.assertTrue('farmer' in joined or 'farm' in joined or 'agriculture' in joined)
        self.assertFalse(match['summary'].lower().startswith('matches your focus'))
        self.assertTrue(match.get('gap_reasons'))

    def test_match_banner_lists_gaps_against_funder_brief(self):
        from webapp.funder_personalization_service import score_cbo_for_funder

        funder = self._funder(
            ['agriculture', 'education'],
            geos=['Kisumu'],
            themes='post-harvest storage, irrigation',
            populations=['school_children'],
        )
        cbo = self._cbo(
            name='Busia Community Tool Hub',
            focus_areas='tools, agriculture, livelihood, community',
            classifications_json='["tools", "agriculture", "livelihood", "community"]',
            cbo_identifier='tools',
            location='Busia County, Kenya',
            county_region='Busia County',
            flagship_summary='Discounted access to gardening tools for community garden projects.',
            success_story='{"title": "Community Story", "summary": "Before the Tool Hub, I spent days digging by hand."}',
        )
        match = score_cbo_for_funder(cbo, funder, {
            'tagline': 'Empowering smallholder farmers and artisans with affordable access to essential tools.',
            'classifications': ['tools', 'agriculture', 'livelihood', 'community'],
            'flagship_summary': 'Discounted access to gardening tools for community garden projects.',
            'success_story': {'title': 'Community Story', 'summary': 'Before the Tool Hub, I spent days digging by hand.'},
            'operational_metric_cards': [
                {'label': 'Living ledger rows', 'value': '69'},
                {'label': 'Farmers', 'value': '12'},
            ],
        })
        self.assertTrue(match['is_match'])
        joined_match = ' '.join(match['reasons']).lower()
        joined_gaps = ' '.join(match['gap_reasons']).lower()
        self.assertNotIn("{'title'", joined_match)
        self.assertNotIn('living ledger', joined_match)
        self.assertIn('education', joined_gaps)
        self.assertIn('kisumu', joined_gaps)
        self.assertTrue('tool' in joined_gaps or 'tools' in joined_gaps)

    def test_about_text_years_of_operation_is_a_gap_for_new_cbos(self):
        from webapp.funder_personalization_service import score_cbo_for_funder

        funder = self._funder(
            ['agriculture'],
            about='Strong leadership team, at least 5 years of operation.',
        )
        cbo = self._cbo(
            name='Busia Community Tool Hub',
            focus_areas='Smallholder agriculture and tool access',
            classifications_json='["agriculture", "tools"]',
            cbo_identifier='tools',
            founded_year='2024',
            chairperson='Jesse Artache',
        )
        match = score_cbo_for_funder(cbo, funder, {
            'tagline': 'Empowering smallholder farmers with affordable tools',
            'founded_year': '2024',
            'classifications': ['agriculture', 'tools'],
        })
        joined = ' '.join(match['gap_reasons']).lower()
        self.assertIn('5 years', joined)
        self.assertIn('2024', joined)
        self.assertIn('leadership', joined)

    def test_established_cbo_meets_years_requirement(self):
        from webapp.funder_personalization_service import score_cbo_for_funder

        funder = self._funder(['agriculture'], about='At least 5 years of operation.')
        cbo = self._cbo(
            name='Kakamega Farm Collective',
            focus_areas='Smallholder agriculture',
            classifications_json='["agriculture"]',
            founded_year='2018',
            chairperson='Maria Rodriguez',
            program_director='Clement Jackson',
        )
        match = score_cbo_for_funder(cbo, funder, {
            'focus_areas': 'Smallholder agriculture',
            'founded_year': '2018',
            'classifications': ['agriculture'],
        })
        joined_gaps = ' '.join(match['gap_reasons']).lower()
        self.assertNotIn('so it has about', joined_gaps)

    def test_placeholder_focus_does_not_tag_agriculture(self):
        from webapp.funder_personalization_service import infer_cbo_interest_tags

        cbo = self._cbo(
            name='Kibera Homework Club',
            focus_areas='Rural livelihood, subsistence agriculture, youth',
            classifications_json='["education"]',
            cbo_identifier='education',
        )
        tags = infer_cbo_interest_tags(cbo, {'focus_areas': 'Rural livelihood, subsistence agriculture, youth'})
        self.assertNotIn('agriculture', tags)

    def test_geo_alone_is_not_a_profile_match(self):
        from webapp.funder_personalization_service import score_cbo_for_funder

        funder = self._funder(['agriculture'], geos=['Busia'])
        cbo = self._cbo(
            name='Busia Homework Club',
            focus_areas='Tutoring and digital literacy',
            classifications_json='["education"]',
            cbo_identifier='education',
            location='Busia County, Kenya',
            county_region='Busia County',
        )
        match = score_cbo_for_funder(cbo, funder, {'classifications': ['education']})
        self.assertFalse(match['is_match'])

    def test_search_plan_stays_independent_of_saved_interests(self):
        from webapp.funder_personalization_service import apply_funder_context_to_search_plan, build_funder_search_context

        context = build_funder_search_context({
            'interest_areas': ['agriculture'],
            'interest_labels': ['Agriculture & food systems'],
            'geographic_focus': ['Busia'],
            'about_text': '',
            'interest_brief': {
                'themes': 'post-harvest storage, farmer cooperatives',
                'populations': ['smallholder_farmers'],
                'outcomes': ['food_security'],
                'evidence_needed': 'acres and farmer reach',
            },
        })
        plan = apply_funder_context_to_search_plan({
            'qualitative_preferences': {'mission_alignment_terms': ['tutoring']},
            'structured_filters': {'text_terms': ['education']},
        }, context)
        self.assertEqual(plan['qualitative_preferences']['mission_alignment_terms'], ['tutoring'])
        self.assertEqual(plan['structured_filters']['text_terms'], ['education'])
        self.assertNotIn('agriculture', ' '.join(plan['qualitative_preferences']['mission_alignment_terms']).lower())


class ExistingCboEnrichmentTests(unittest.TestCase):
    def test_placeholder_focus_is_sparse(self):
        self.assertTrue(_is_sparse_profile_value('Rural livelihood, subsistence agriculture, youth'))
        self.assertFalse(_is_sparse_profile_value('Youth livelihoods in Kisumu'))

    def test_highlights_do_not_duplicate(self):
        profile = {}
        _append_unique_highlight(profile, 'Trained 80 farmers')
        _append_unique_highlight(profile, 'Trained 80 farmers')
        self.assertEqual(profile['web_highlights'], ['Trained 80 farmers'])

    def test_existing_custom_focus_is_kept_and_gaps_are_filled(self):
        from unittest.mock import patch
        import json

        class ExistingCBO:
            name = 'Acme Youth Group'
            focus_areas = 'Custom youth apprenticeships'
            location = 'Kisumu'
            founded_year = '2014'
            flagship_summary = 'Seed program'
            success_story = 'Farmers trained'
            classifications_json = '["youth"]'
            ai_profile_json = json.dumps({
                'tagline': 'Training youth',
                'focus_areas': 'Custom youth apprenticeships',
                'flagship_project': {'title': 'Seed program', 'summary': 'Seed program'},
            })
            web_research_json = '{}'
            web_research_verified_at = None
            web_research_refreshed_at = None

        research = {
            'status': 'verified',
            'claims': [{
                'id': 1,
                'headline': 'Won a county award',
                'fact': 'Won a county award in 2023',
                'verified': True,
            }],
            'profile_suggestions': {
                'focus_areas': 'Agriculture and water',
                'website': 'https://example.org',
                'classifications': ['agriculture'],
            },
        }
        cbo = ExistingCBO()
        with patch('webapp.cbo_web_research_service.db.session.add'):
            profile = apply_web_research_to_cbo(cbo, research, only_verified=True)
        self.assertEqual(cbo.focus_areas, 'Custom youth apprenticeships')
        self.assertEqual(profile['website'], 'https://example.org')
        self.assertEqual(set(json.loads(cbo.classifications_json)), {'youth', 'agriculture'})
        self.assertIn('Won a county award in 2023', profile['web_highlights'])

    def test_placeholder_focus_is_replaced_when_confirmed(self):
        from unittest.mock import patch
        import json

        class SparseCBO:
            name = 'Acme Youth Group'
            focus_areas = 'Rural livelihood, subsistence agriculture, youth'
            location = ''
            founded_year = ''
            flagship_summary = ''
            success_story = ''
            classifications_json = '[]'
            ai_profile_json = '{}'
            web_research_json = '{}'
            web_research_verified_at = None
            web_research_refreshed_at = None

        research = {
            'status': 'verified',
            'claims': [{'id': 1, 'headline': 'Works with farmers', 'verified': True}],
            'profile_suggestions': {'focus_areas': 'Youth livelihoods in Kisumu'},
        }
        cbo = SparseCBO()
        with patch('webapp.cbo_web_research_service.db.session.add'):
            apply_web_research_to_cbo(cbo, research, only_verified=True)
        self.assertEqual(cbo.focus_areas, 'Youth livelihoods in Kisumu')


class WebSearchFallbackTests(unittest.TestCase):
    def test_gemini_key_enables_search_without_custom_search(self):
        from flask import Flask
        from webapp.google_search_service import google_custom_search_configured, google_search_enabled

        app = Flask('search-fallback-test')
        app.config['GOOGLE_SEARCH_API_KEY'] = ''
        app.config['GOOGLE_SEARCH_ENGINE_ID'] = ''
        app.config['GEMINI_API_KEY'] = ''
        with app.app_context():
            self.assertFalse(google_custom_search_configured())
            self.assertTrue(google_search_enabled())

    def test_grounded_sources_keep_the_official_cbo_site(self):
        from webapp.google_search_service import _items_from_grounded_sources, guess_official_website

        items = _items_from_grounded_sources(
            'KIWA',
            [
                {'title': 'Kisumu West Aid', 'url': 'https://www.kisumuwestaid.org/about'},
                {'title': 'Profile', 'url': 'https://aseworkshop.com/social-entrepreneur-of-the-month-september-2023/'},
            ],
            'https://www.kisumuwestaid.org/',
            'Kisumu West Aid (KIWA) is a CBO in Kisumu.',
        )
        self.assertEqual(items[0]['link'], 'https://www.kisumuwestaid.org/about')
        self.assertTrue(items[0]['is_official'])
        self.assertIn('kisumuwestaid.org', guess_official_website('KIWA', items))

    def test_bing_html_unwraps_the_result_url(self):
        import base64
        from webapp.google_search_service import parse_bing_html

        token = base64.b64encode(b'https://kiwa-kenya.org/about').decode()
        html = (
            f'<h2><a href="https://www.bing.com/ck/a?u=a1{token}&amp;ntb=1">KIWA Kenya</a></h2>'
            '<p>A community group in Busia.</p>'
        )
        items = parse_bing_html(html)
        self.assertEqual(items[0]['link'], 'https://kiwa-kenya.org/about')
        self.assertIn('Busia', items[0]['snippet'])

    def test_search_uses_bing_when_duckduckgo_is_blocked(self):
        from unittest.mock import patch
        from flask import Flask
        from webapp.google_search_service import search_google

        app = Flask('bing-fallback-test')
        app.config['GOOGLE_SEARCH_API_KEY'] = ''
        app.config['GOOGLE_SEARCH_ENGINE_ID'] = ''
        bing_items = [{
            'title': 'KIWA community group',
            'link': 'https://kiwa-kenya.org/about',
            'display_link': 'kiwa-kenya.org',
            'snippet': 'A community-based organisation in western Kenya.',
        }]
        with app.app_context():
            with patch(
                'webapp.google_search_service._search_duckduckgo',
                return_value={'items': [], 'error': 'blocked', 'source': 'duckduckgo'},
            ), patch(
                'webapp.google_search_service._search_bing',
                return_value={'items': bing_items, 'error': '', 'source': 'bing'},
            ):
                result = search_google('KIWA Kenya CBO')
        self.assertEqual(result['source'], 'bing')
        self.assertEqual(result['items'][0]['link'], 'https://kiwa-kenya.org/about')
        self.assertEqual(result['error'], '')

    def test_parse_duckduckgo_html_extracts_results(self):
        from webapp.google_search_service import parse_duckduckgo_html

        html = '''
        <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fsites.google.com%2Fview%2Fhealthyhappysisters%2Fhome">Healthy Happy Sisters</a>
        <a class="result__snippet">A Kisumu community group.</a>
        '''
        items = parse_duckduckgo_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'Healthy Happy Sisters')
        self.assertIn('sites.google.com', items[0]['link'])

    def test_research_surfaces_the_real_search_error(self):
        from unittest.mock import patch
        from flask import Flask
        from webapp.cbo_web_research_service import research_cbo_online

        app = Flask('search-error-test')
        app.config['GOOGLE_SEARCH_API_KEY'] = ''
        app.config['GOOGLE_SEARCH_ENGINE_ID'] = ''
        app.config['GEMINI_API_KEY'] = ''
        with app.app_context():
            with patch(
                'webapp.cbo_web_research_service.search_google',
                return_value={'items': [], 'error': 'Google Custom Search is not configured.', 'source': 'none'},
            ):
                result = research_cbo_online('Healthy Happy Sisters', 'Kisumu')
        self.assertIn('not configured', result['error'])
        self.assertEqual(result['claims'], [])

    def test_research_builds_claims_from_search_items(self):
        from unittest.mock import patch
        from flask import Flask
        from webapp.cbo_web_research_service import research_cbo_online

        app = Flask('search-items-test')
        app.config['GOOGLE_SEARCH_API_KEY'] = ''
        app.config['GOOGLE_SEARCH_ENGINE_ID'] = ''
        app.config['GEMINI_API_KEY'] = ''
        items = [{
            'title': 'Healthy Happy Sisters supports girls in Kisumu',
            'link': 'https://example.org/hhs',
            'display_link': 'example.org',
            'snippet': 'A Kisumu CBO working on menstrual health and GBV response.',
        }]
        with app.app_context():
            with patch(
                'webapp.cbo_web_research_service.search_google',
                return_value={'items': items, 'error': '', 'source': 'duckduckgo'},
            ), patch(
                'webapp.cbo_web_research_service.enrich_research_corpus',
                side_effect=lambda name, found, **kwargs: found,
            ):
                result = research_cbo_online('Healthy Happy Sisters', 'Kisumu')
        self.assertEqual(len(result['claims']), 1)
        self.assertIn('Kisumu', result['claims'][0]['fact'])
        self.assertTrue(any('founder' in query for query in result['queries']))

    def test_page_excerpt_skips_nav_and_keeps_team(self):
        from webapp.google_search_service import discover_profile_links, extract_page_excerpt, guess_official_website

        html = '''
        <html><body>
          <nav><a href="/home">Home</a><a href="/donate">Donate</a></nav>
          <header>Cookie banner subscribe now</header>
          <main>
            <h1>About Kijenzi</h1>
            <p>John Gershenson CO-FOUNDER leads the organisation with Ben Savonen CO-FOUNDER.</p>
            <a href="/about/">About</a>
            <a href="/team/">Our Team</a>
          </main>
        </body></html>
        '''
        excerpt = extract_page_excerpt(html)
        self.assertIn('John Gershenson', excerpt)
        self.assertNotIn('Cookie banner', excerpt)
        links = discover_profile_links(html, 'https://www.kijenzi.com/')
        self.assertTrue(any('/about' in link for link in links))
        self.assertTrue(any('/team' in link for link in links))
        official = guess_official_website('Kijenzi', [
            {'title': 'Kijenzi', 'link': 'https://www.kijenzi.com/about/', 'snippet': 'Our team'},
            {'title': 'News', 'link': 'https://nation.africa/kijenzi', 'snippet': 'A story'},
        ])
        self.assertIn('kijenzi.com', official)

    def test_page_sections_keep_team_and_story_after_a_long_intro(self):
        from webapp.google_search_service import extract_page_excerpt, extract_page_sections

        filler = 'Kijenzi hosts community workshops across the county. ' * 80
        html = f'''<html><body><main>
          <h2>Welcome</h2><p>{filler}</p>
          <h2>Our Team</h2><p>John Gershenson, Co-founder. Ben Savonen, Co-founder.</p>
          <h2>Our Story</h2><p>Kijenzi began in Kisumu to print medical supplies.</p>
        </main></body></html>'''
        sections = extract_page_sections(html)
        kinds = {section['kind'] for section in sections}
        self.assertIn('team', kinds)
        self.assertIn('history', kinds)
        excerpt = extract_page_excerpt(html)
        self.assertIn('John Gershenson', excerpt)
        self.assertIn('began in Kisumu', excerpt)
        self.assertNotIn('workshops across the county', excerpt)

    def test_directory_listings_and_header_cards_are_kept(self):
        from webapp.google_search_service import _html_is_thin, extract_page_excerpt, extract_page_sections

        listing = '''<html><body>
          <dl>
            <dt>Address</dt><dd>Okore Road, Kisumu</dd>
            <dt>Established</dt><dd>Kijenzi was established in 2017 to print medical supplies.</dd>
          </dl>
        </body></html>'''
        sections = extract_page_sections(listing)
        blob = ' '.join(section['text'] for section in sections)
        self.assertIn('Okore Road, Kisumu', blob)
        self.assertIn('established in 2017', blob)
        self.assertFalse(_html_is_thin(listing))

        card = '''<html><body><header>
          <h1>Kijenzi directory</h1>
          <p>The Kijenzi hub sits on Okore Road in Kisumu and prints medical supplies for nearby hospitals every week.</p>
        </header></body></html>'''
        self.assertIn('Okore Road', extract_page_excerpt(card))

        shell = '''<html><head>
          <script type="application/ld+json">{"name":"Kijenzi","description":"A lab"}</script>
        </head><body></body></html>'''
        self.assertTrue(_html_is_thin(shell))

        rendered = '''<html><head>
          <script type="application/ld+json">{"name":"Kijenzi"}</script>
        </head><body><main>
          <h3>3D printing in Kisumu, Kenya</h3>
          <div>Kijenzi 3D prints low-risk medical devices and spare parts for hospitals in Kisumu. They keep a catalog of items local clinics can order.</div>
          <h3>Prof. Jenny Dankelman</h3>
          <div>Project lead</div>
        </main></body></html>'''
        rendered_text = extract_page_excerpt(rendered)
        self.assertIn('catalog of items', rendered_text)
        self.assertIn('Jenny Dankelman', rendered_text)
        self.assertFalse(_html_is_thin(rendered))
        wall = '<html><body><h1>Performing security verification</h1><p>This page is displayed while the website verifies you are not a bot.</p></body></html>'
        self.assertTrue(_html_is_thin(wall))

    def test_indexed_snippet_queries_target_walled_hosts(self):
        from webapp.google_search_service import indexed_snippet_queries

        queries = indexed_snippet_queries('Kijenzi', [
            {'link': 'https://www.facebook.com/kijenzi/'},
            {'link': 'https://pitchbook.com/profiles/company/279173-71'},
            {'link': 'https://www.kijenzi.com/about/'},
            {'link': 'https://m.linkedin.com/company/kijenzi'},
        ])
        self.assertEqual(queries, [
            '"Kijenzi" site:facebook.com',
            '"Kijenzi" site:pitchbook.com',
            '"Kijenzi" site:linkedin.com',
        ])

    def test_mediawiki_fetch_reads_the_article_api(self):
        from unittest.mock import MagicMock, patch
        from webapp.google_search_service import _fetch_mediawiki_html, _mediawiki_title

        link = 'https://wiki.p2pfoundation.net/Kijenzi_-_Distributed_Manufacturing_in_Western_Kenya'
        self.assertEqual(_mediawiki_title(link), 'Kijenzi_-_Distributed_Manufacturing_in_Western_Kenya')
        response = MagicMock()
        response.status_code = 200
        response.content = b'{}'
        response.json.return_value = {
            'parse': {'text': {'*': '<h2>History</h2><p>Kijenzi set up the first manufacturing hub in Kisumu after two years of pilot studies.</p>'}},
        }
        with patch('webapp.google_search_service.requests.get', return_value=response):
            html = _fetch_mediawiki_html(link)
        self.assertIn('manufacturing hub in Kisumu', html)

    def test_synthesis_pages_keep_named_sections_and_drop_other_orgs(self):
        from webapp.cbo_web_research_service import select_synthesis_pages

        pages = select_synthesis_pages('Kijenzi', [
            {
                'title': 'Other Org',
                'link': 'https://example.org/about',
                'sections': [{'heading': 'About', 'kind': 'about', 'text': 'Another charity works in Nairobi.'}],
            },
            {
                'title': 'Kijenzi team',
                'link': 'https://kijenzi.com/team',
                'sections': [{'heading': 'Our Team', 'kind': 'team', 'text': 'Kijenzi co-founder John Gershenson leads the team.'}],
            },
            {
                'title': 'Kijenzi story',
                'link': 'https://kijenzi.com/story',
                'sections': [{'heading': 'Our Story', 'kind': 'history', 'text': 'Kijenzi began in Kisumu.'}],
            },
        ])
        links = [page['link'] for page in pages]
        self.assertEqual(links, ['https://kijenzi.com/team', 'https://kijenzi.com/story'])

    def test_grounding_keeps_quoted_facts_and_drops_invented_names(self):
        from webapp.cbo_web_research_service import _ground_model_output, _normalize_claims

        pages = [{
            'link': 'https://kijenzi.com/team',
            'sections': [{
                'heading': 'Our Team',
                'kind': 'team',
                'text': 'Kijenzi was founded in 2017. John Gershenson is a co-founder.',
            }],
        }]
        grounded = _ground_model_output('Kijenzi', {
            'summary': 'Kijenzi prints medical supplies.',
            'claims': [
                {
                    'headline': 'Co-founder',
                    'fact': 'John Gershenson is a co-founder of Kijenzi.',
                    'evidence_quote': 'John Gershenson is a co-founder',
                    'section_heading': 'Our Team',
                    'source_url': 'https://kijenzi.com/team/',
                    'category': 'founder',
                },
                {
                    'headline': 'Wrong person',
                    'fact': 'Mary Ann is the founder.',
                    'evidence_quote': 'Mary Ann founded the lab in Nairobi',
                    'section_heading': 'Our Team',
                    'source_url': 'https://kijenzi.com/team',
                    'category': 'founder',
                },
            ],
            'profile_suggestions': {
                'founder': 'Our Team John Gershenson',
                'team': [{'name': 'Mary Ann', 'role': 'Founder'}],
                'founded_year': '2017',
                'history': 'The group started as a bakery in Mombasa.',
            },
        }, pages)
        self.assertEqual(len(grounded['claims']), 1)
        self.assertEqual(grounded['claims'][0]['headline'], 'Co-founder')
        self.assertEqual(grounded['profile_suggestions']['founder'], 'John Gershenson')
        self.assertEqual(grounded['profile_suggestions']['founded_year'], '2017')
        self.assertNotIn('team', grounded['profile_suggestions'])
        self.assertNotIn('history', grounded['profile_suggestions'])
        borrowed = _normalize_claims(
            [{'headline': 'Founded in 2017', 'fact': 'Founded in 2017.'}],
            [{'link': 'https://example.org/other'}],
        )
        self.assertEqual(borrowed[0]['source_url'], '')

    def test_confirmed_people_and_history_file_into_the_fluid_profile(self):
        from unittest.mock import patch
        import json
        from webapp.cbo_web_research_service import serialize_web_research_for_display

        class SparseCBO:
            name = 'Kijenzi'
            focus_areas = 'Rural livelihood, subsistence agriculture, youth'
            location = ''
            founded_year = ''
            flagship_summary = ''
            success_story = ''
            chairperson = ''
            program_director = ''
            finance_lead = ''
            classifications_json = '[]'
            ai_profile_json = '{}'
            web_research_json = '{}'
            web_research_verified_at = None
            web_research_refreshed_at = None

        research = {
            'status': 'ready',
            'summary': 'Kijenzi 3D-prints medical supplies in Kisumu.',
            'claims': [{
                'id': 1,
                'headline': 'Co-founder: John Gershenson',
                'fact': 'John Gershenson is a co-founder of Kijenzi.',
                'category': 'founder',
                'verified': True,
            }],
            'profile_suggestions': {
                'founder': 'John Gershenson',
                'team': [
                    {'name': 'John Gershenson', 'role': 'Co-founder'},
                    {'name': 'Ben Savonen', 'role': 'Co-founder'},
                ],
                'history': 'It began with malaria microscope knobs in western Kenya.',
                'location': 'Kisumu, Kenya',
                'website': 'https://www.kijenzi.com',
                'founded_year': '2017',
            },
        }
        cbo = SparseCBO()
        with patch('webapp.cbo_web_research_service.db.session.add'):
            profile = apply_web_research_to_cbo(
                cbo,
                research,
                only_verified=True,
                suggestion_keys=['founder', 'team', 'history', 'location', 'website', 'founded_year'],
            )
        self.assertEqual(cbo.location, 'Kisumu, Kenya')
        self.assertEqual(cbo.program_director, 'John Gershenson')
        self.assertTrue(profile['origin_story'].startswith('It began with malaria'))
        leaders = profile['narrative_profile']['leadership']
        self.assertEqual({person['name'] for person in leaders}, {'John Gershenson', 'Ben Savonen'})
        roles = {section['role'] for section in profile['narrative_profile']['sections']}
        self.assertIn('leadership', roles)
        self.assertIn('identity', roles)
        self.assertEqual(research['applied_receipt']['latest_count'], 7)
        display = serialize_web_research_for_display(research, cbo=cbo)
        self.assertFalse(display['pending_review'])
        self.assertEqual(display['claims'], [])
        self.assertEqual(display['applied_receipt']['latest_count'], 7)

    def test_unchecked_claims_stay_pending_after_partial_confirm(self):
        updated = apply_verification_decision(
            {
                'claims': [
                    {'id': 1, 'headline': 'Founded in 2017'},
                    {'id': 2, 'headline': 'Team in Kisumu'},
                ],
                'status': 'ready',
            },
            {'decision': 'partial', 'verified_ids': [1], 'rejected_ids': []},
        )
        by_id = {item['id']: item for item in updated['claims']}
        self.assertTrue(by_id[1]['verified'])
        self.assertFalse(by_id[2]['verified'])
        self.assertFalse(by_id[2]['rejected'])

    def test_namesake_company_adds_no_profile_facts(self):
        from unittest.mock import patch
        from flask import Flask
        from webapp.cbo_web_research_service import research_cbo_online

        app = Flask('identity-reject-test')
        app.config['GEMINI_API_KEY'] = 'test-key'
        items = [{
            'title': 'Kiwa | Testing, Inspection and Certification',
            'link': 'https://www.kiwa.com/en/about-kiwa/',
            'snippet': 'Kiwa is a Dutch global testing company. Luc Leroy is the CEO.',
        }]
        reason = (
            'These pages describe Kiwa, a Dutch testing and certification company, '
            'which is a different organisation from the Kenyan CBO.'
        )
        with app.app_context():
            with patch(
                'webapp.cbo_web_research_service.search_google',
                return_value={'items': items, 'error': '', 'source': 'duckduckgo'},
            ), patch(
                'webapp.cbo_web_research_service._filter_items_by_identity',
                return_value=([], reason),
            ):
                result = research_cbo_online(
                    'KIWA',
                    '',
                    extra_context='Known focus: Rural livelihood, subsistence agriculture, youth',
                )
        self.assertTrue(result['identity_mismatch'])
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['profile_suggestions'], {})
        self.assertIn('Dutch', result['summary'])
        self.assertTrue(any('CBO' in query or 'community' in query.lower() for query in result['queries']))

    def test_identity_screen_keeps_the_cbo_and_drops_the_company(self):
        import json
        from unittest.mock import patch
        from flask import Flask
        from webapp.cbo_web_research_service import _filter_items_by_identity

        app = Flask('identity-screen-test')
        app.config['GEMINI_API_KEY'] = 'test-key'
        items = [
            {
                'title': 'KIWA community group',
                'link': 'https://kiwa-kenya.org/about',
                'snippet': 'KIWA is a community-based organisation in western Kenya working with smallholder farmers.',
            },
            {
                'title': 'Kiwa',
                'link': 'https://www.kiwa.com/',
                'snippet': 'Kiwa is a Dutch certification company.',
            },
        ]
        body = {
            'matches_found': True,
            'reason': 'Only the Kenya community page is this CBO.',
            'matching_urls': ['https://kiwa-kenya.org/about/'],
        }

        class FakeModels:
            def generate_content(self, **kwargs):
                class Response:
                    text = json.dumps(body)
                return Response()

        class FakeClient:
            def __init__(self, **kwargs):
                self.models = FakeModels()

        with app.app_context():
            with patch('webapp.cbo_web_research_service.genai.Client', FakeClient):
                kept, reason = _filter_items_by_identity('KIWA', '', '', items)
        self.assertEqual(reason, '')
        self.assertEqual([item['link'] for item in kept], ['https://kiwa-kenya.org/about'])

    def test_mismatch_summary_does_not_keep_namesake_facts(self):
        import json
        from unittest.mock import patch
        from flask import Flask
        from webapp.cbo_web_research_service import _synthesize_research

        app = Flask('identity-synth-test')
        app.config['GEMINI_API_KEY'] = 'test-key'
        items = [{
            'title': 'About Kiwa',
            'link': 'https://www.kiwa.com/en/about-kiwa/',
            'snippet': 'Kiwa is a Dutch testing company.',
            'sections': [{
                'heading': 'About',
                'kind': 'about',
                'text': (
                    'Kiwa is a Dutch global testing, inspection and certification company. '
                    'Luc Leroy is the CEO. Founded in 1948 by Dutch drinking water companies.'
                ),
            }],
        }]
        body = {
            'same_organisation': False,
            'summary': (
                'The provided sections describe Kiwa, a Dutch global testing company, '
                'which is a different organisation from the Kenyan CBO.'
            ),
            'claims': [{
                'headline': 'CEO',
                'fact': 'Luc Leroy is the CEO.',
                'evidence_quote': 'Luc Leroy is the CEO',
                'section_heading': 'About',
                'source_url': 'https://www.kiwa.com/en/about-kiwa/',
            }],
            'profile_suggestions': {
                'website': 'https://www.kiwa.com',
                'team': [{'name': 'Luc Leroy', 'role': 'CEO'}],
                'history': 'Founded in 1948 by Dutch drinking water companies.',
            },
        }

        class FakeModels:
            def generate_content(self, **kwargs):
                class Response:
                    text = json.dumps(body)
                return Response()

        class FakeClient:
            def __init__(self, **kwargs):
                self.models = FakeModels()

        with app.app_context():
            with patch('webapp.cbo_web_research_service.genai.Client', FakeClient):
                result = _synthesize_research(
                    'KIWA',
                    '',
                    'Known focus: Rural livelihood, subsistence agriculture, youth',
                    items,
                )
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['profile_suggestions'], {})
        self.assertIn('different organisation', result['summary'])

    def test_placeholder_focus_is_not_treated_as_a_known_fact(self):
        from webapp.cbo_web_research_service import _profile_context_for_model

        context = _profile_context_for_model(
            'Known focus: Rural livelihood, subsistence agriculture, youth\n'
            'Known location: Busia, Kenya\n'
            'Director: Program manager'
        )
        self.assertNotIn('Rural livelihood', context)
        self.assertNotIn('Program manager', context)
        self.assertIn('Busia, Kenya', context)


    def test_program_cards_and_photos_are_pulled_from_a_page(self):
        from webapp.google_search_service import extract_content_images, extract_page_programs, extract_page_sections

        html = '''<html><head><title>Kisumu West Aid</title></head><body>
          <main><h1>Our Programs</h1><p>Short intro.</p></main>
          <img alt="Education Empowerment" src="/_next/image?url=%2Fimages%2Fprogramme1.jpeg&amp;w=640&amp;q=75">
          <h2>Education Empowerment</h2>
          <p>We support access to quality education through scholarships and school partnerships.</p>
          <img alt="logo" src="/images/logo.svg">
          <script>{"title":"Youth Empowerment","description":"Equipping young people with skills and entrepreneurship support.","image":"/images/programme3.jpeg"}</script>
        </body></html>'''
        programs = extract_page_programs(html, 'https://www.kisumuwestaid.org/programs')
        self.assertEqual(programs[0]['name'], 'Youth Empowerment')
        self.assertEqual(programs[0]['image_url'], 'https://www.kisumuwestaid.org/images/programme3.jpeg')
        images = extract_content_images(html, 'https://www.kisumuwestaid.org/programs', programs)
        urls = [image['url'] for image in images]
        self.assertIn('https://www.kisumuwestaid.org/images/programme1.jpeg', urls)
        self.assertIn('https://www.kisumuwestaid.org/images/programme3.jpeg', urls)
        self.assertTrue(all('logo' not in url for url in urls))
        headings = {section['heading'] for section in extract_page_sections(html)}
        self.assertIn('Education Empowerment', headings)
        self.assertIn('Youth Empowerment', headings)

    def test_confirmed_programs_and_photos_land_in_program_overview(self):
        from unittest.mock import patch

        class SparseCBO:
            id = 33
            name = 'KIWA'
            focus_areas = ''
            location = ''
            founded_year = ''
            flagship_summary = ''
            success_story = ''
            chairperson = ''
            program_director = ''
            finance_lead = ''
            classifications_json = '[]'
            ai_profile_json = '{}'
            web_research_json = '{}'
            web_research_verified_at = None
            web_research_refreshed_at = None

        research = {
            'status': 'ready',
            'claims': [],
            'media': [{
                'id': 'photo1',
                'url': 'https://www.kisumuwestaid.org/images/programme1.jpeg',
                'program': 'Education Empowerment',
            }],
            'profile_suggestions': {
                'programs': [{
                    'name': 'Education Empowerment',
                    'description': 'We support access to quality education through scholarships and school partnerships.',
                    'who_it_serves': 'Students in Kisumu West',
                }],
            },
        }
        cbo = SparseCBO()
        from flask import Flask
        app = Flask('program-photo-test')
        app.config['PROGRAM_PHOTO_UPLOAD_DIR'] = '/tmp/kumbu-program-photos'
        with app.app_context(), \
                patch('webapp.cbo_web_research_service.db.session.add'), \
                patch('webapp.cbo_web_research_service.download_public_image', return_value=(b'\xff\xd8\xff' + b'x' * 3000, 'image/jpeg', 'jpg')), \
                patch('webapp.cbo_web_research_service.store_supporting_file', return_value={
                    'storage_backend': 'local',
                    'stored_path': 'program_photos/education-empowerment.jpg',
                    'storage_bucket': '',
                    'storage_object_path': '',
                }):
            profile = apply_web_research_to_cbo(
                cbo,
                research,
                only_verified=False,
                suggestion_keys=['program:education-empowerment'],
                media_ids=['photo1'],
            )
        programs = profile['narrative_profile']['programs']
        self.assertEqual(programs[0]['name'], 'Education Empowerment')
        self.assertIn('scholarships', programs[0]['description'])
        photos = profile['narrative_profile']['program_media']['education-empowerment']
        self.assertEqual(photos[0]['stored_path'], 'program_photos/education-empowerment.jpg')
        self.assertEqual(research['applied_media_ids'], ['photo1'])

    def test_profile_logo_is_taken_from_the_official_site(self):
        import base64
        from webapp.google_search_service import extract_content_images, extract_profile_images, _raster_embedded_in_svg
        from webapp.cbo_web_research_service import _profile_images_from_pages

        html = '''<html><body>
          <img alt="Logo" src="/images/logo_kiwa-01.svg">
          <img alt="Education Empowerment" src="/images/programme1.jpeg">
        </body></html>'''
        logos = extract_profile_images(html, 'https://www.kisumuwestaid.org/')
        self.assertEqual(logos[0]['url'], 'https://www.kisumuwestaid.org/images/logo_kiwa-01.svg')
        photos = [image['url'] for image in extract_content_images(html, 'https://www.kisumuwestaid.org/')]
        self.assertNotIn('https://www.kisumuwestaid.org/images/logo_kiwa-01.svg', photos)
        chosen = _profile_images_from_pages([
            {
                'is_official': True,
                'link': 'https://www.kisumuwestaid.org/',
                'profile_images': logos,
            },
            {
                'link': 'https://www.kiwa.com/',
                'profile_images': [{'url': 'https://www.kiwa.com/logo.png', 'alt': 'Logo'}],
            },
        ])
        self.assertEqual(len(chosen), 1)
        self.assertIn('kisumuwestaid.org', chosen[0]['url'])
        png = b'\x89PNG\r\n\x1a\n' + (b'x' * 2500)
        svg = f'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,{base64.b64encode(png).decode()}"/></svg>'
        raster = _raster_embedded_in_svg(svg.encode(), max_bytes=2_000_000)
        self.assertEqual(raster[1], 'image/png')
        self.assertTrue(raster[0].startswith(b'\x89PNG'))

    def test_confirmed_logo_is_stored_on_the_profile(self):
        from unittest.mock import patch
        from flask import Flask

        class SparseCBO:
            id = 33
            name = 'KIWA'
            focus_areas = ''
            location = ''
            founded_year = ''
            flagship_summary = ''
            success_story = ''
            chairperson = ''
            program_director = ''
            finance_lead = ''
            classifications_json = '[]'
            ai_profile_json = '{}'
            web_research_json = '{}'
            web_research_verified_at = None
            web_research_refreshed_at = None

        research = {
            'status': 'ready',
            'claims': [],
            'profile_images': [{
                'id': 'logo1',
                'url': 'https://www.kisumuwestaid.org/images/logo_kiwa-01.svg',
                'alt': 'Logo',
            }],
            'profile_suggestions': {},
        }
        app = Flask('profile-image-test')
        app.config['PROGRAM_PHOTO_UPLOAD_DIR'] = '/tmp/kumbu-program-photos'
        cbo = SparseCBO()
        with app.app_context(), \
                patch('webapp.cbo_web_research_service.db.session.add'), \
                patch('webapp.cbo_web_research_service.download_profile_image', return_value=(b'\x89PNG\r\n\x1a\n' + b'x' * 3000, 'image/png', 'png')), \
                patch('webapp.cbo_web_research_service.store_supporting_file', return_value={
                    'storage_backend': 'local',
                    'stored_path': 'profile_images/profile-image.png',
                    'storage_bucket': '',
                    'storage_object_path': '',
                }):
            profile = apply_web_research_to_cbo(
                cbo,
                research,
                only_verified=False,
                suggestion_keys=[],
                media_ids=[],
                profile_image_id='logo1',
            )
        self.assertEqual(profile['profile_image']['stored_path'], 'profile_images/profile-image.png')
        self.assertEqual(profile['profile_image']['mime_type'], 'image/png')
        self.assertEqual(research['applied_profile_image_id'], 'logo1')

    def test_dated_public_facts_become_separate_milestones_and_a_future_step(self):
        from unittest.mock import patch

        class SparseCBO:
            name = 'KIWA'
            focus_areas = ''
            location = ''
            founded_year = ''
            flagship_summary = ''
            success_story = ''
            chairperson = ''
            program_director = ''
            finance_lead = ''
            classifications_json = '[]'
            ai_profile_json = '{}'
            web_research_json = '{}'
            web_research_verified_at = None
            web_research_refreshed_at = None

        research = {
            'status': 'ready',
            'claims': [{
                'id': 1,
                'headline': 'Founded and registered',
                'fact': 'Kisumu West Aid was founded in 2015 and gained official registration in 2021.',
                'category': 'history',
                'verified': True,
            }],
            'profile_suggestions': {
                'history': 'Kisumu West Aid was founded in 2015 and gained official registration in 2021.',
                'founded_year': '2015',
                'future_goals': [{
                    'when': '2027',
                    'title': 'Open a community skills centre',
                    'summary': 'KIWA plans to open a community skills centre in Kisumu West in 2027.',
                    'status': 'planned',
                }],
            },
        }
        cbo = SparseCBO()
        with patch('webapp.cbo_web_research_service.db.session.add'):
            profile = apply_web_research_to_cbo(
                cbo,
                research,
                only_verified=True,
                suggestion_keys=['history', 'goal:2027-open-a-community-skills-centre'],
            )
        past = profile['narrative_profile']['journey']['past']
        future = profile['narrative_profile']['journey']['future']
        self.assertEqual([step['title'] for step in past], ['Organisation founded', 'Official registration'])
        self.assertEqual([step['when'] for step in past], ['2015', '2021'])
        self.assertEqual(future[0]['title'], 'Open a community skills centre')
        self.assertEqual(future[0]['status'], 'planned')


if __name__ == '__main__':
    unittest.main()
