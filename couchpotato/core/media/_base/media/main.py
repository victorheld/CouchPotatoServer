import traceback
from couchpotato import tryInt, get_db
from couchpotato.api import addApiView
from couchpotato.core.event import fireEvent, fireEventAsync, addEvent
from couchpotato.core.helpers.encoding import toUnicode
from couchpotato.core.helpers.variable import splitString, getImdb, getTitle
from couchpotato.core.logger import CPLog
from couchpotato.core.media import MediaBase
from .index import MediaIMDBIndex, MediaStatusIndex, MediaTypeIndex, TitleSearchIndex, TitleIndex, StartsWithIndex
from string import ascii_lowercase

log = CPLog(__name__)


class MediaPlugin(MediaBase):

    _database = {
        'media': MediaIMDBIndex,
        'media_search_title': TitleSearchIndex,
        'media_status': MediaStatusIndex,
        'media_by_type': MediaTypeIndex,
        'media_title': TitleIndex,
        'media_startswith': StartsWithIndex,
    }

    def __init__(self):

        addApiView('media.refresh', self.refresh, docs = {
            'desc': 'Refresh a any media type by ID',
            'params': {
                'id': {'desc': 'Movie, Show, Season or Episode ID(s) you want to refresh.', 'type': 'int (comma separated)'},
            }
        })

        addApiView('media.list', self.listView, docs = {
            'desc': 'List media',
            'params': {
                'type': {'type': 'string', 'desc': 'Media type to filter on.'},
                'status': {'type': 'array or csv', 'desc': 'Filter movie by status. Example:"active,done"'},
                'release_status': {'type': 'array or csv', 'desc': 'Filter movie by status of its releases. Example:"snatched,available"'},
                'limit_offset': {'desc': 'Limit and offset the movie list. Examples: "50" or "50,30"'},
                'starts_with': {'desc': 'Starts with these characters. Example: "a" returns all movies starting with the letter "a"'},
                'search': {'desc': 'Search movie title'},
            },
            'return': {'type': 'object', 'example': """{
    'success': True,
    'empty': bool, any movies returned or not,
    'media': array, media found,
}"""}
        })

        addApiView('media.get', self.getView, docs = {
            'desc': 'Get media by id',
            'params': {
                'id': {'desc': 'The id of the media'},
            }
        })

        addApiView('media.delete', self.deleteView, docs = {
            'desc': 'Delete a media from the wanted list',
            'params': {
                'id': {'desc': 'Media ID(s) you want to delete.', 'type': 'int (comma separated)'},
                'delete_from': {'desc': 'Delete media from this page', 'type': 'string: all (default), wanted, manage'},
            }
        })

        addApiView('media.available_chars', self.charView)

        addEvent('app.load', self.addSingleRefreshView, priority = 100)
        addEvent('app.load', self.addSingleListView, priority = 100)
        addEvent('app.load', self.addSingleCharView, priority = 100)
        addEvent('app.load', self.addSingleDeleteView, priority = 100)

        addEvent('media.get', self.get)
        addEvent('media.list', self.list)
        addEvent('media.delete', self.delete)
        addEvent('media.restatus', self.restatus)

    def refresh(self, id = '', **kwargs):
        handlers = []
        ids = splitString(id)

        for x in ids:

            refresh_handler = self.createRefreshHandler(x)
            if refresh_handler:
                handlers.append(refresh_handler)

        fireEvent('notify.frontend', type = 'media.busy', data = {'_id': ids})
        fireEventAsync('schedule.queue', handlers = handlers)

        return {
            'success': True,
        }

    def createRefreshHandler(self, media_id):

        try:
            media = get_db().get('id', media_id)


            default_title = getTitle(media)
            event = '%s.update_info' % media.get('type')

            def handler():
                fireEvent(event, identifier = media.get('identifier'), default_title = default_title, on_complete = self.createOnComplete(media_id))

            if handler:
                return handler

        except:
            log.error('Refresh handler for non existing media: %s', traceback.format_exc())


    def addSingleRefreshView(self):

        for media_type in fireEvent('media.types', merge = True):
            addApiView('%s.refresh' % media_type, self.refresh)

    def get(self, media_id):

        db = get_db()

        imdb_id = getImdb(str(media_id))

        if imdb_id:
            m = db.get('media', imdb_id, with_doc = True)['doc']
        else:
            m = db.get('id', media_id)

        results = None
        if m:
            results = db.run('media', 'to_dict', m['_id'])

        return results

    def getView(self, id = None, **kwargs):

        media = self.get(id) if id else None

        return {
            'success': media is not None,
            'media': media,
        }

    def list(self, types = None, status = None, release_status = None, limit_offset = None, starts_with = None, search = None):

        db = get_db()

        # Make a list from string
        if status and not isinstance(status, (list, tuple)):
            status = [status]
        if release_status and not isinstance(release_status, (list, tuple)):
            release_status = [release_status]
        if types and not isinstance(types, (list, tuple)):
            types = [types]

        # query media ids
        if types:
            all_media_ids = set()
            for media_type in types:
                all_media_ids = all_media_ids.union(set([x['_id'] for x in db.get_many('media_by_type', media_type)]))
        else:
            all_media_ids = set([x['_id'] for x in db.all('media')])

        media_ids = list(all_media_ids)
        filter_by = {}

        # Filter on movie status
        if status and len(status) > 0:
            filter_by['media_status'] = set()
            for media_status in db.run('media', 'with_status', status, with_doc = False):
                filter_by['media_status'].add(media_status.get('_id'))

        # Filter on release status
        if release_status and len(release_status) > 0:
            filter_by['release_status'] = set()
            for release_status in db.run('release', 'with_status', release_status, with_doc = False):
                filter_by['release_status'].add(release_status.get('media_id'))

        # Add search filters
        if starts_with:
            filter_by['starts_with'] = set()
            starts_with = toUnicode(starts_with.lower())[0]
            starts_with = starts_with if starts_with in ascii_lowercase else '#'
            filter_by['starts_with'] = [x['_id'] for x in db.get_many('media_startswith', starts_with)]

        # Filter with search query
        if search:
            filter_by['search'] = [x['_id'] for x in db.get_many('media_search_title', search)]

        # Filter by combining ids
        for x in filter_by:
            media_ids = [n for n in media_ids if n in filter_by[x]]

        total_count = len(media_ids)
        if total_count == 0:
            return 0, []

        offset = 0
        limit = -1
        if limit_offset:
            splt = splitString(limit_offset) if isinstance(limit_offset, (str, unicode)) else limit_offset
            limit = tryInt(splt[0])
            offset = tryInt(0 if len(splt) is 1 else splt[1])

        # List movies based on title order
        medias = []
        for m in db.all('media_title'):
            media_id = m['_id']
            if media_id not in media_ids: continue
            if offset > 0:
                offset -= 1
                continue

            media = db.run('media', 'to_dict', media_id)

            media['releases'] = list(db.run('release', 'for_media', media_id))

            # Merge releases with movie dict
            medias.append(media)

            # remove from media ids
            media_ids.remove(media_id)
            if len(media_ids) == 0 or len(medias) == limit: break

        return total_count, medias

    def listView(self, **kwargs):

        types = splitString(kwargs.get('type'))
        status = splitString(kwargs.get('status'))
        release_status = splitString(kwargs.get('release_status'))
        limit_offset = kwargs.get('limit_offset')
        starts_with = kwargs.get('starts_with')
        search = kwargs.get('search')

        total_movies, movies = self.list(
            types = types,
            status = status,
            release_status = release_status,
            limit_offset = limit_offset,
            starts_with = starts_with,
            search = search
        )

        return {
            'success': True,
            'empty': len(movies) == 0,
            'total': total_movies,
            'movies': movies,
        }

    def addSingleListView(self):

        for media_type in fireEvent('media.types', merge = True):
            def tempList(*args, **kwargs):
                return self.listView(types = media_type, *args, **kwargs)
            addApiView('%s.list' % media_type, tempList)

    def availableChars(self, types = None, status = None, release_status = None):

        db = get_db()

        # Make a list from string
        if status and not isinstance(status, (list, tuple)):
            status = [status]
        if release_status and not isinstance(release_status, (list, tuple)):
            release_status = [release_status]
        if types and not isinstance(types, (list, tuple)):
            types = [types]

        # query media ids
        if types:
            all_media_ids = set()
            for media_type in types:
                all_media_ids = all_media_ids.union(set([x['_id'] for x in db.get_many('media_by_type', media_type)]))
        else:
            all_media_ids = set([x['_id'] for x in db.all('media')])

        media_ids = all_media_ids
        filter_by = {}

        # Filter on movie status
        if status and len(status) > 0:
            filter_by['media_status'] = set()
            for media_status in db.run('media', 'with_status', status, with_doc = False):
                filter_by['media_status'].add(media_status.get('_id'))

        # Filter on release status
        if release_status and len(release_status) > 0:
            filter_by['release_status'] = set()
            for release_status in db.run('release', 'with_status', release_status, with_doc = False):
                filter_by['release_status'].add(release_status.get('media_id'))

        # Filter by combining ids
        for x in filter_by:
            media_ids = [n for n in media_ids if n in filter_by[x]]

        chars = set()
        for x in db.all('media_startswith'):
            if x['_id'] in media_ids:
                chars.add(x['key'])

            if len(chars) == 25:
                break

        return list(chars)

    def charView(self, **kwargs):

        type = splitString(kwargs.get('type', 'movie'))
        status = splitString(kwargs.get('status', None))
        release_status = splitString(kwargs.get('release_status', None))
        chars = self.availableChars(type, status, release_status)

        return {
            'success': True,
            'empty': len(chars) == 0,
            'chars': chars,
        }

    def addSingleCharView(self):

        for media_type in fireEvent('media.types', merge = True):
            def tempChar(*args, **kwargs):
                return self.charView(types = media_type, *args, **kwargs)
            addApiView('%s.available_chars' % media_type, tempChar)

    def delete(self, media_id, delete_from = None):

        try:
            db = get_db()

            media = db.get('id', media_id)
            if media:
                deleted = False
                if delete_from == 'all':
                    db.delete(media)
                    deleted = True
                else:

                    media_releases = list(db.run('release', 'for_media', media['_id']))

                    total_releases = len(media_releases)
                    total_deleted = 0
                    new_media_status = None

                    for release in media_releases:
                        if delete_from in ['wanted', 'snatched', 'late']:
                            if release.get('status') != 'done':
                                db.delete(release)
                                total_deleted += 1
                            new_media_status = 'done'
                        elif delete_from == 'manage':
                            if release.get('status') == 'done':
                                db.delete(release)
                                total_deleted += 1
                            new_media_status = 'active'

                    if total_releases == total_deleted:
                        db.delete(media)
                        deleted = True
                    elif new_media_status:
                        media['status'] = new_media_status
                        db.update(media)
                    else:
                        fireEvent('media.restatus', media.get('_id'), single = True)

                if deleted:
                    fireEvent('notify.frontend', type = 'media.deleted', data = media)
        except:
            log.error('Failed deleting media: %s', traceback.format_exc())

        return True

    def deleteView(self, id = '', **kwargs):

        ids = splitString(id)
        for media_id in ids:
            self.delete(media_id, delete_from = kwargs.get('delete_from', 'all'))

        return {
            'success': True,
        }

    def addSingleDeleteView(self):

        for media_type in fireEvent('media.types', merge = True):
            def tempDelete(*args, **kwargs):
                return self.deleteView(types = media_type, *args, **kwargs)
            addApiView('%s.delete' % media_type, tempDelete)

    def restatus(self, media_id):

        try:
            db = get_db()

            m = db.get('id', media_id)

            log.debug('Changing status for %s', getTitle(m))
            if not m['profile_id']:
                m['status'] = 'done'
            else:
                move_to_wanted = True

                profile = db.get('id', m['profile_id'])
                media_releases = db.run('release', 'for_media', m['_id'])

                for q_identifier in profile['qualities']:
                    index = profile['qualities'].index(q_identifier)

                    for release in media_releases:
                        if q_identifier is release['quality'] and (release.get('status') == 'done' and profile['finish'][index]):
                            move_to_wanted = False

                m['status'] = 'active' if move_to_wanted else 'done'

            db.update(m)

            return True
        except:
            log.error('Failed restatus: %s', traceback.format_exc())
