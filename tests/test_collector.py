import unittest

from bs4 import BeautifulSoup

from src import collector


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.event_source = {
            "name": "Test Events",
            "url": "https://events.example/",
            "type": "events",
            "category": "Music",
        }
        self.news_source = {
            "name": "Test News",
            "url": "https://news.example/",
            "type": "wordpress",
            "category": "Tourism",
        }
        self.meaningful_description = (
            "The event brings local musicians together for an evening "
            "of live performances, with a program celebrating Brazilian "
            "music and the artists who shaped its history."
        )

    def test_general_news_never_gets_an_event_date_and_uses_url_category(self):
        content = (
            "The city announced a public program on 20/11/2026. "
            "The article was published with information for residents."
        )

        article = collector.build_article(
            self.news_source,
            "City announcement",
            "https://news.example/saude/city-announcement/",
            content,
            event_date="2026-11-20",
        )

        self.assertEqual(article["event_date"], "")
        self.assertEqual(article["category"], "Health")

    def test_general_news_without_matching_path_defaults_to_news(self):
        article = collector.build_article(
            self.news_source,
            "City announcement",
            "https://news.example/noticias/city-announcement/",
            "The city announced a public program with information for residents.",
            event_date="2026-11-20",
        )

        self.assertEqual(article["category"], "News")
        self.assertEqual(article["event_date"], "")

    def test_general_news_keeps_only_an_explicit_event_date(self):
        article = collector.build_article(
            self.news_source,
            "Festival announcement",
            "https://news.example/cultura/festival-announcement/",
            (
                "Published on 20/11/2026. "
                "The festival will take place on 31/12/2026 in Copacabana."
            ),
            event_date="2026-11-20",
        )

        self.assertEqual(article["event_date"], "2026-12-31")

    def test_general_news_parses_english_event_dates_only_with_years(self):
        explicit_date = collector.extract_explicit_event_date(
            "The concert will take place on December 31, 2026."
        )
        missing_year = collector.extract_explicit_event_date(
            "The concert will take place on December 31."
        )

        self.assertEqual(explicit_date, "2026-12-31")
        self.assertEqual(missing_year, "")

    def test_jsonld_event_without_detail_url_is_not_assigned_listing_url(self):
        soup = BeautifulSoup(
            """
            <script type="application/ld+json">
            {
              "@type": "Event",
              "name": "A Brazilian musicians concert",
              "description": "The event brings local musicians together "
                "for an evening of live performances and Brazilian music."
            }
            </script>
            """,
            "html.parser",
        )

        self.assertEqual(
            collector.extract_jsonld_events(soup, self.event_source),
            [],
        )

    def test_jsonld_event_keeps_its_distinct_detail_url(self):
        soup = BeautifulSoup(
            f"""
            <script type="application/ld+json">
            {{
              "@type": "Event",
              "name": "A Brazilian musicians concert",
              "url": "https://events.example/event/unique-event/",
              "description": "{self.meaningful_description}"
            }}
            </script>
            """,
            "html.parser",
        )

        articles = collector.extract_jsonld_events(soup, self.event_source)

        self.assertEqual(len(articles), 1)
        self.assertEqual(
            articles[0]["source_url"],
            "https://events.example/event/unique-event/",
        )

    def test_event_validation_rejects_ticket_and_promotion_only_content(self):
        content = (
            "Compre seu ingresso agora! Garanta seu ingresso, confira "
            "ingressos e saiba mais. Não perca essa oportunidade imperdível! "
        ) * 4

        article = collector.build_article(
            self.event_source,
            "A concert",
            "https://events.example/a-concert/",
            content,
        )

        self.assertIsNone(article)

    def test_event_validation_keeps_specific_description(self):
        article = collector.build_article(
            self.event_source,
            "Brazilian music concert",
            "https://events.example/a-concert/",
            self.meaningful_description,
        )

        self.assertIsNotNone(article)

    def test_event_validation_rejects_long_unrelated_text(self):
        content = (
            "Rio de Janeiro has many neighborhoods with different "
            "architectural styles, historic streets, museums, parks, "
            "beaches, restaurants, and public spaces for residents and "
            "visitors to explore throughout the year. "
        ) * 5

        article = collector.build_article(
            self.event_source,
            "A Brazilian musicians concert",
            "https://events.example/a-concert/",
            content,
        )

        self.assertIsNone(article)

    def test_image_extraction_supports_lazy_srcset_and_backgrounds(self):
        srcset_soup = BeautifulSoup(
            '<div><img data-srcset="/small.jpg 300w, /large.jpg 900w"></div>',
            "html.parser",
        )
        self.assertEqual(
            collector.extract_image(srcset_soup.div, "https://events.example/"),
            "https://events.example/large.jpg",
        )

        background_soup = BeautifulSoup(
            '<div style="background-image:url(\'/poster.jpg\')"></div>',
            "html.parser",
        )
        self.assertEqual(
            collector.extract_image(
                background_soup.div, "https://events.example/"
            ),
            "https://events.example/poster.jpg",
        )

    def test_image_extraction_supports_page_metadata_and_video_posters(self):
        page = BeautifulSoup(
            """
            <head>
              <meta property="og:image" content="/og.jpg">
              <meta name="twitter:image" content="/twitter.jpg">
            </head>
            <main><video poster="/poster.jpg"></video></main>
            """,
            "html.parser",
        )
        self.assertEqual(
            collector.extract_page_image(
                page, "https://events.example/article/"
            ),
            "https://events.example/og.jpg",
        )
        self.assertEqual(
            collector.extract_image(
                page.main, "https://events.example/article/"
            ),
            "https://events.example/poster.jpg",
        )

    def test_tracking_pixel_is_not_accepted_as_an_article_image(self):
        image_url = collector.normalize_image_url(
            "https://www.facebook.com/tr?id=123&ev=PageView&noscript=1",
            "https://events.example/article/",
        )

        self.assertEqual(image_url, "")

    def test_article_image_falls_back_to_the_article_body(self):
        soup = BeautifulSoup(
            """
            <article>
              <img src="/article-image.jpg">
              <p>This paragraph contains enough useful article content.</p>
            </article>
            """,
            "html.parser",
        )

        _, image_url = collector.extract_article_from_soup(
            soup, "https://events.example/article/"
        )

        self.assertEqual(
            image_url,
            "https://events.example/article-image.jpg",
        )

    def test_card_image_does_not_walk_to_unrelated_parent(self):
        soup = BeautifulSoup(
            """
            <div><img src="/site-header.jpg">
              <article><h2>A concert</h2></article>
            </div>
            """,
            "html.parser",
        )

        self.assertEqual(
            collector.extract_card_image(
                soup.article, "https://events.example/"
            ),
            "",
        )

    def test_card_image_uses_nearest_wrapper_with_its_own_detail_link(self):
        soup = BeautifulSoup(
            """
            <section>
              <img src="/event-poster.jpg">
              <a href="/events/concert"><div class="title">
                <h2>A concert</h2>
              </div></a>
            </section>
            """,
            "html.parser",
        )

        self.assertEqual(
            collector.extract_card_image(
                soup.select_one(".title"),
                "https://events.example/",
                detail_url="https://events.example/events/concert",
            ),
            "https://events.example/event-poster.jpg",
        )

    def test_card_link_skips_placeholder_links_to_the_listing_page(self):
        card = BeautifulSoup(
            """
            <article>
              <a href="#">Saiba mais</a>
              <a href="/events/concert">Saiba mais</a>
            </article>
            """,
            "html.parser",
        ).article

        self.assertEqual(
            collector.get_card_link(card, "https://events.example/"),
            "https://events.example/events/concert",
        )

    def test_vivo_subtitle_prefix_is_removed_without_rewriting_title(self):
        self.assertEqual(
            collector._clean_vivo_subtitle(
                "Apresenta: Jesus, o homem que dividiu o tempo."
            ),
            "Jesus, o homem que dividiu o tempo.",
        )
        self.assertEqual(
            collector._clean_vivo_subtitle("Frisson Livre"),
            "Frisson Livre",
        )

    def test_vivo_card_title_uses_artist_heading_and_event_subtitle(self):
        card = BeautifulSoup(
            """
            <article>
              <h2>Leandro Karnal</h2>
              <h2>Apresenta: Jesus, o homem que dividiu o tempo.</h2>
              <h2>21 | fev –</h2>
              <h2>17h00</h2>
            </article>
            """,
            "html.parser",
        ).article

        self.assertEqual(
            collector._vivo_card_title(card),
            "Leandro Karnal — Jesus, o homem que dividiu o tempo.",
        )


if __name__ == "__main__":
    unittest.main()
