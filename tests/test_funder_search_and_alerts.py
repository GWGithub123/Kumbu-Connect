import json
import unittest

from webapp.funder_alert_service import (
    build_new_cbo_email,
    build_update_email,
    classify_peer_post,
    highlight_items_from_profile,
    new_profile_highlights,
    photos_for_update,
    select_feed_cards,
)
from webapp.funder_personalization_service import text_overlaps_funder_interests
from webapp.gemini_service import explain_search_match


class _Profile:
    def __init__(self, interests, themes=''):
        self.organization_name = 'Test Funder'
        self.website = ''
        self.country_region = 'Kenya'
        self.about_text = ''
        self.interest_areas_json = json.dumps(interests)
        self.geographic_focus_json = '[]'
        self.grant_size_min = None
        self.grant_size_max = None
        self.grant_currency = 'KES'
        self.preference_memory_json = '{}'
        self.interest_brief_json = json.dumps({
            'themes': themes,
            'populations': [],
            'outcomes': [],
            'evidence_needed': '',
        })


def _candidate(slug, name, focus, classification):
    return {
        'slug': slug,
        'name': name,
        'location': 'Kenya',
        'tagline': focus,
        'focus_areas': focus,
        'flagship_summary': focus,
        'classifications': [classification],
        'badge': 'silver',
        'score': 60,
        'total_revenue': 0,
        'revenue_growth': 0,
        'avg_rating': 7,
        'responses': 3,
        'recent_quotes': [],
    }


class SearchMatchIndependenceTests(unittest.TestCase):
    def test_education_search_outranks_an_agriculture_profile(self):
        query = 'education tutoring programs for students'
        education = explain_search_match(query, _candidate(
            'edu', 'Kibera Homework Club', 'education tutoring literacy students', 'education',
        ))
        agriculture = explain_search_match(query, _candidate(
            'farm', 'Kakamega Farm Collective', 'smallholder agriculture irrigation farmers', 'agriculture',
        ))
        self.assertGreater(education['score'], agriculture['score'])
        self.assertIn('education', ' '.join(education['reasons']).lower() + education['query'])

    def test_update_text_follows_saved_interests_not_the_other_sector(self):
        agriculture = _Profile(['agriculture'], themes='farmer cooperatives')
        education = _Profile(['education'])
        note = 'New tutoring sessions helped students finish homework.'
        self.assertFalse(text_overlaps_funder_interests(note, agriculture)['is_match'])
        self.assertTrue(text_overlaps_funder_interests(note, education)['is_match'])
        self.assertIn('education', text_overlaps_funder_interests(note, education)['hits'])


class HighlightFeedTests(unittest.TestCase):
    def test_only_new_profile_text_is_treated_as_an_update(self):
        old = {
            'web_highlights': ['Shared tools with 40 farmers in Kakamega.'],
            'success_story': {'summary': 'Farmers harvested more after the training.'},
        }
        new = {
            'web_highlights': [
                'Shared tools with 40 farmers in Kakamega.',
                'Opened a new irrigation plot for smallholder farmers.',
            ],
            'success_story': {'summary': 'Farmers harvested more after the training.'},
        }
        added = new_profile_highlights(json.dumps(old), json.dumps(new), 'Kakamega Farm Collective')
        bodies = ' '.join(item['body'] for item in added).lower()
        self.assertIn('irrigation', bodies)
        self.assertNotIn('harvested more', bodies)
        self.assertEqual(len(highlight_items_from_profile(old, 'Kakamega Farm Collective')), 2)

    def test_alert_emails_link_to_the_profile_and_outreach(self):
        subject, body = build_new_cbo_email(
            cbo_name='Kakamega Farm Collective',
            match_summary='This fits agriculture because they train farmers.',
            interest_labels=['Agriculture & food systems'],
            profile_url='https://kumbu.example/cbo/kakamega-farm',
            contact_url='https://kumbu.example/cbo/kakamega-farm#contact-funder',
        )
        self.assertIn('Kakamega Farm Collective', subject)
        self.assertIn('https://kumbu.example/cbo/kakamega-farm', body)
        self.assertIn('#contact-funder', body)
        self.assertIn('separately', body.lower())

        update_subject, update_body = build_update_email(
            cbo_name='Kakamega Farm Collective',
            title='Profile highlight',
            body_text='Opened a new irrigation plot for smallholder farmers.',
            interest_labels=['Agriculture & food systems'],
            profile_url='https://kumbu.example/cbo/kakamega-farm',
            contact_url='https://kumbu.example/cbo/kakamega-farm#contact-funder',
        )
        self.assertIn('update', update_subject.lower())
        self.assertIn('irrigation', update_body)
        self.assertIn('#contact-funder', update_body)

    def test_profile_updates_keep_recent_program_photos(self):
        profile = {
            'narrative_profile': {
                'programs': [{'key': 'clinics', 'name': 'Clinic supply'}],
                'program_media': {
                    'clinics': [{
                        'id': 'photo-new',
                        'stored_path': 'photos/new.jpg',
                        'created_at': '2026-10-01T00:00:00',
                    }],
                    'workshops': [{
                        'id': 'photo-old',
                        'stored_path': 'photos/old.jpg',
                        'created_at': '2026-01-01T00:00:00',
                    }],
                },
            },
        }
        matched, recent = photos_for_update(profile, 'Clinic supply hubs are printing parts.')
        self.assertEqual([photo['id'] for photo in matched], ['photo-new'])
        self.assertEqual(recent[0]['id'], 'photo-new')
        empty_matched, fallback = photos_for_update(profile, 'A general profile note.')
        self.assertEqual(empty_matched, [])
        self.assertEqual([photo['id'] for photo in fallback], ['photo-new', 'photo-old'])

    def test_feed_keeps_at_most_two_posts_per_organisation(self):
        candidates = []
        for index in range(5):
            candidates.append({'cbo_id': 1, 'body': f'Kijenzi update {index}', 'title': 'Update'})
        candidates.append({'cbo_id': 2, 'body': 'KIWA health work', 'title': 'Update'})
        candidates.append({'cbo_id': 1, 'body': 'Kijenzi update 0', 'title': 'Repeat'})
        selected = select_feed_cards(candidates, limit=12)
        self.assertEqual([item['cbo_id'] for item in selected], [1, 2, 1])
        self.assertEqual(selected[0]['body'], 'Kijenzi update 0')
        self.assertEqual(selected[2]['body'], 'Kijenzi update 1')

    def test_profile_writeup_leads_when_program_posts_are_newer(self):
        candidates = [
            {'cbo_id': 33, 'kind': 'program', 'body': 'Household livelihoods update'},
            {'cbo_id': 33, 'kind': 'program', 'body': 'Youth empowerment update'},
            {'cbo_id': 34, 'kind': 'program', 'body': 'Another livelihoods note'},
            {'cbo_id': 33, 'kind': 'highlight', 'body': 'KIWA provides livelihood and agricultural support.'},
        ]
        selected = select_feed_cards(candidates, limit=12)
        self.assertEqual(
            [(item['cbo_id'], item['kind']) for item in selected],
            [(33, 'highlight'), (34, 'program'), (33, 'program')],
        )

    def test_a_post_with_responses_leads_that_organisation(self):
        candidates = [
            {'cbo_id': 34, 'kind': 'narrative', 'activity_id': 191, 'body': 'KIWA profile writeup for funders.', 'like_count': 0, 'comment_count': 0},
            {'cbo_id': 34, 'kind': 'program', 'activity_id': 199, 'body': 'Household livelihoods update with a like.', 'like_count': 1, 'comment_count': 0},
            {'cbo_id': 34, 'kind': 'program', 'activity_id': 198, 'body': 'Health and water update with a comment.', 'like_count': 1, 'comment_count': 1},
            {'cbo_id': 1, 'kind': 'highlight', 'activity_id': 1, 'body': 'Another organisation update.', 'like_count': 0, 'comment_count': 0},
        ]
        selected = select_feed_cards(candidates, limit=2)
        self.assertEqual([item['activity_id'] for item in selected], [198, 199])


class PeerFeedLaneTests(unittest.TestCase):
    def test_region_overlap_wins_over_similar_work(self):
        self.assertEqual(
            classify_peer_post({'busia'}, {'health'}, {'busia'}, {'health'}),
            'region',
        )

    def test_similar_work_is_used_when_regions_differ(self):
        self.assertEqual(
            classify_peer_post({'busia'}, {'health'}, {'kisumu'}, {'health'}),
            'similar',
        )

    def test_empty_overlap_stays_out_of_both_lanes(self):
        self.assertEqual(classify_peer_post(set(), set(), {'busia'}, {'health'}), '')
        self.assertEqual(classify_peer_post({'busia'}, {'health'}, set(), set()), '')
        self.assertEqual(classify_peer_post({'busia'}, set(), {'kisumu'}, set()), '')


class AlertCommitTests(unittest.TestCase):
    def test_saving_a_profile_change_does_not_fail_the_commit(self):
        import json
        from flask import Flask
        from webapp.funder_alert_service import register_funder_alert_listeners
        from webapp.models import CBO, db

        app = Flask('alert-commit-test')
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        app.config['FUNDER_INTEREST_ALERTS'] = False
        db.init_app(app)
        with app.app_context():
            register_funder_alert_listeners()
            db.create_all()
            cbo = CBO(name='KIWA', slug='kiwa-alert-commit', ai_profile_json='{}')
            db.session.add(cbo)
            db.session.commit()
            cbo.ai_profile_json = json.dumps({
                'web_highlights': ['KIWA trains smallholder farmers in Busia.'],
            })
            db.session.commit()
            saved = db.session.get(CBO, cbo.id)
            self.assertIn('smallholder farmers', saved.ai_profile_json)

    def test_new_matching_cbo_emails_the_preferred_contact(self):
        import json
        from unittest.mock import patch
        from flask import Flask
        from webapp.funder_alert_service import register_funder_alert_listeners
        from webapp.models import CBO, FunderInterestAlert, FunderProfile, User, db

        app = Flask('alert-email-test')
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        app.config['FUNDER_INTEREST_ALERTS'] = True
        app.config['PUBLIC_BASE_URL'] = 'https://kumbu.example'
        app.add_url_rule('/cbo/<slug>', endpoint='main.cbo_profile', view_func=lambda slug: '')
        db.init_app(app)
        with app.app_context():
            register_funder_alert_listeners()
            db.create_all()
            funder = User(
                email='login@example.com',
                password_hash='x',
                role=User.ROLE_FUNDER,
                display_name='Test Funder',
                account_status='active',
            )
            db.session.add(funder)
            db.session.commit()
            db.session.add(FunderProfile(
                user_id=funder.id,
                interest_areas_json=json.dumps(['healthcare']),
                contact_email='alerts@example.com',
            ))
            db.session.commit()
            with patch('webapp.funder_alert_service.send_gmail_message') as send:
                cbo = CBO(
                    name='Clinic Collective',
                    slug='clinic-collective-alert',
                    focus_areas='community health clinic',
                    ai_profile_json=json.dumps({
                        'tagline': 'Runs a community health clinic in Busia.',
                        'focus_areas': 'community health clinic',
                    }),
                )
                db.session.add(cbo)
                db.session.commit()
                self.assertTrue(send.called)
                recipient, subject, body = send.call_args.args
                self.assertEqual(recipient, 'alerts@example.com')
                self.assertIn('Clinic Collective', subject)
                self.assertIn('View the profile:', body)
                self.assertIn('Reach out', body)
                alert = FunderInterestAlert.query.filter_by(user_id=funder.id).one()
                self.assertEqual(alert.status, 'sent')

                send.reset_mock()
                cbo.ai_profile_json = json.dumps({
                    'tagline': 'Runs a community health clinic in Busia.',
                    'focus_areas': 'community health clinic',
                    'web_highlights': ['Opened a new community health clinic for remote patients.'],
                })
                db.session.commit()
                self.assertTrue(send.called)
                self.assertEqual(send.call_args.args[0], 'alerts@example.com')
                self.assertIn('update', send.call_args.args[1].lower())
