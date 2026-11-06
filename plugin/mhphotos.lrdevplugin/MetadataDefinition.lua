-- Own fields shown in Lightroom's metadata panel and usable in filters and smart collections.
-- Change schemaVersion when fields are added or removed.
return {
  metadataFieldsForPhotos = {
    {
      id = 'status',
      title = 'MHPhotos-Status',
      dataType = 'enum',
      searchable = true,
      browsable = true,
      values = {
        { value = 'insta_kandidat', title = 'Insta-Kandidat' },
        { value = 'fotobuch', title = 'Fotobuch' },
        { value = 'veroeffentlicht', title = 'Veröffentlicht' },
        { value = 'aussortiert', title = 'Aussortiert (Vorschlag)' },
      },
    },
    {
      id = 'notiz',
      title = 'MHPhotos-Notiz',
      dataType = 'string',
      searchable = true,
      browsable = false,
    },
  },
  schemaVersion = 1,
}
